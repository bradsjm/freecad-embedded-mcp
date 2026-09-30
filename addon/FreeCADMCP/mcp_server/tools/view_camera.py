"""Camera geometry primitives for the ``capture_view`` and
``inspect_user_context`` tools.

Pure mathematics only: tuple-vector arithmetic, axis-angle and quaternion
conversions, Inventor camera-string parsing and splicing, live camera axes
read from a view, basis-axis and screen-up selection, and the shared
three-float reader. There is no module-level FreeCAD import: the one
native touchpoint, the guarded ``Rotation`` preference in
``_rotation_quat``, imports FreeCAD lazily as its first statement, so the
module loads and behaves identically in production, under headless test
doubles, and inside the native-contract probe.
"""

from __future__ import annotations

import math
from typing import Any

# ---------------------------------------------------------------------------
# Small tuple-vector helpers. Direction/up/normal math stays on plain
# floats; FreeCAD.Vector is only constructed where a native API call needs
# one (camera strings, clipping placements).
# ---------------------------------------------------------------------------


def _finite_vec(values: Any) -> bool:
    """True when ``values`` is three finite floats."""

    try:
        return len(values) == 3 and all(math.isfinite(float(value)) for value in values)
    except (TypeError, ValueError):
        return False


def _vec_negate(values: tuple[float, float, float]) -> tuple[float, float, float]:
    """Return the negation of a three-float vector."""
    return (-values[0], -values[1], -values[2])


def _vec_dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    """Return the dot product of two three-float vectors."""
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _vec_add(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    """Return the componentwise sum of two three-float vectors."""
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _vec_sub(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    """Return the componentwise difference ``a - b``."""
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _vec_scale(values: tuple[float, float, float], factor: float) -> tuple[float, float, float]:
    """Return the vector scaled componentwise by ``factor``."""
    return (values[0] * factor, values[1] * factor, values[2] * factor)


def _vec_cross(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    """Return the cross product of two three-float vectors."""
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _vec_norm(values: tuple[float, float, float]) -> float:
    """Return the Euclidean length of a three-float vector."""
    return math.sqrt(_vec_dot(values, values))


def _vec_normalize(values: tuple[float, float, float]) -> tuple[float, float, float] | None:
    """Return the unit vector, or None when zero-length or non-finite."""
    norm = _vec_norm(values)
    if not math.isfinite(norm) or norm <= 0.0:
        return None
    return (values[0] / norm, values[1] / norm, values[2] / norm)


def _unit_axis(index: int) -> tuple[float, float, float]:
    """Return the unit basis vector along the axis at ``index``."""
    return tuple(1.0 if position == index else 0.0 for position in range(3))  # type: ignore[return-value]


# Document basis axes in tie-break order (X before Y before Z): the least
# parallel one becomes a derived screen-up or section normal.
_BASIS_AXES = (_unit_axis(0), _unit_axis(1), _unit_axis(2))
_BASIS_NAMES = ("X", "Y", "Z")


def _camera_keyword_values(
    camera: str, keyword: str, count: int
) -> tuple[list[float], int, int] | None:
    """Read ``count`` numbers after ``keyword`` in a camera string.

    Handles both the native ``position 1 2 3`` form and a parenthesized
    ``position (1,2,3)`` form. Returns the values plus the exact
    character span they occupy so ``_derived_camera_string`` can splice
    replacements in place — the native ``setCamera`` rejects a rewritten
    camera whose original whitespace was not preserved, so the rest of
    the string must stay byte-identical. ``None`` when the keyword is
    absent or its value tuple is malformed.
    """

    search_from = 0
    while True:
        index = camera.find(keyword, search_from)
        if index < 0:
            return None
        # The keyword must stand alone as a word.
        before = camera[index - 1] if index > 0 else " "
        after_at = index + len(keyword)
        after = camera[after_at] if after_at < len(camera) else " "
        if not (before.isspace() or before in "{,(") or not (after.isspace() or after in "},("):
            search_from = index + 1
            continue
        cursor = after_at
        values: list[float] = []
        span_start: int | None = None
        span_end = cursor
        while len(values) < count:
            # Skip separators between values; stop at the first character
            # that cannot start a number.
            while cursor < len(camera) and (camera[cursor].isspace() or camera[cursor] in "(),"):
                cursor += 1
            run_start = cursor
            while cursor < len(camera) and (camera[cursor].isdigit() or camera[cursor] in "+-.eE"):
                cursor += 1
            if cursor == run_start:
                break
            try:
                value = float(camera[run_start:cursor])
            except ValueError:
                break
            if not math.isfinite(value):
                break
            values.append(value)
            if span_start is None:
                span_start = run_start
            span_end = cursor
        if len(values) == count:
            return values, span_start or 0, span_end
        return None


def _parse_camera(camera: str) -> dict[str, tuple[list[float], int, int]] | None:
    """Parse ``position`` and ``orientation`` (and optional
    ``focalDistance``) from a camera string; ``None`` when unparseable."""

    parsed: dict[str, tuple[list[float], int, int]] = {}
    if not camera:
        return None
    for keyword, count in (("position", 3), ("orientation", 4)):
        found = _camera_keyword_values(camera, keyword, count)
        if found is None:
            return None
        parsed[keyword] = found
    focal = _camera_keyword_values(camera, "focalDistance", 1)
    if focal is not None:
        parsed["focalDistance"] = focal
    return parsed


def _rotate_axis_angle(
    axis: tuple[float, float, float], angle: float, vec: tuple[float, float, float]
) -> tuple[float, float, float] | None:
    """Rotate ``vec`` by the axis-angle pair (Rodrigues' formula).

    Inventor camera strings write ``orientation`` as an axis-angle (unit
    axis plus radians), not as a quaternion; the axis is normalized here
    so a non-unit axis still rotates correctly.
    """

    norm = _vec_norm(axis)
    if not math.isfinite(norm) or norm <= 0.0 or not math.isfinite(angle):
        return None
    unit = (axis[0] / norm, axis[1] / norm, axis[2] / norm)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    dot = _vec_dot(unit, vec)
    cross = _vec_cross(unit, vec)
    return tuple(
        vec[index] * cosine + cross[index] * sine + unit[index] * dot * (1.0 - cosine)
        for index in range(3)
    )


def _quat_to_axis_angle(
    quat: tuple[float, float, float, float],
) -> tuple[tuple[float, float, float], float]:
    """Axis-angle (unit axis, radians in [0, pi]) from a unit quaternion."""

    qx, qy, qz, qw = quat
    norm = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    if norm <= 0.0:
        return (0.0, 0.0, 1.0), 0.0
    qx, qy, qz, qw = qx / norm, qy / norm, qz / norm, qw / norm
    angle = 2.0 * math.acos(max(-1.0, min(1.0, qw)))
    sin_half = math.sqrt(max(0.0, 1.0 - qw * qw))
    if sin_half <= 0.0:
        return (0.0, 0.0, 1.0), 0.0
    axis = (qx / sin_half, qy / sin_half, qz / sin_half)
    if angle > math.pi:
        angle = 2.0 * math.pi - angle
        axis = _vec_negate(axis)
    return axis, angle


def _camera_axes(view: Any) -> tuple[list[float], list[float]] | None:
    """Live camera ``(direction, up)`` unit vectors from ``getCamera()``.

    ``direction`` is the view direction (the camera-local -Z axis in world
    coordinates) and ``up`` the camera-local +Y axis. The camera
    ``orientation`` is read as the axis-angle pair Inventor writes.
    Returns ``None`` when the camera string is unavailable or
    unparseable; panel manifests then report null camera axes instead of
    failing the capture.
    """

    try:
        camera = str(view.getCamera())
    except Exception:
        return None
    parsed = _parse_camera(camera)
    if parsed is None:
        return None
    axis_values, angle = parsed["orientation"][0][:3], parsed["orientation"][0][3]
    direction = _rotate_axis_angle(axis_values, angle, (0.0, 0.0, -1.0))
    up = _rotate_axis_angle(axis_values, angle, (0.0, 1.0, 0.0))
    if not _finite_vec(direction) or not _finite_vec(up):
        return None
    return list(direction), list(up)


def _quat_from_basis(
    x: tuple[float, float, float],
    y: tuple[float, float, float],
    z: tuple[float, float, float],
) -> tuple[float, float, float, float]:
    """Rotation quaternion ``(x, y, z, w)`` from a unit orthogonal basis.

    The basis vectors are the camera X/Y/Z axes in world coordinates
    (matrix columns); Shepperd's method picks the numerically largest
    quaternion component.
    """

    # The basis vectors are the matrix columns (camera X/Y/Z axes in
    # world coordinates), not the rows.
    m00, m10, m20 = x
    m01, m11, m21 = y
    m02, m12, m22 = z
    trace = m00 + m11 + m22
    if trace > 0.0:
        s = 2.0 * math.sqrt(trace + 1.0)
        return (
            (m21 - m12) / s,
            (m02 - m20) / s,
            (m10 - m01) / s,
            0.25 * s,
        )
    if m00 > m11 and m00 > m22:
        s = 2.0 * math.sqrt(1.0 + m00 - m11 - m22)
        return (
            0.25 * s,
            (m01 + m10) / s,
            (m02 + m20) / s,
            (m21 - m12) / s,
        )
    if m11 > m22:
        s = 2.0 * math.sqrt(1.0 + m11 - m00 - m22)
        return (
            (m01 + m10) / s,
            0.25 * s,
            (m12 + m21) / s,
            (m02 - m20) / s,
        )
    s = 2.0 * math.sqrt(1.0 + m22 - m00 - m11)
    return (
        (m02 + m20) / s,
        (m12 + m21) / s,
        0.25 * s,
        (m10 - m01) / s,
    )


def _rotation_quat(
    x: tuple[float, float, float],
    y: tuple[float, float, float],
    z: tuple[float, float, float],
) -> tuple[float, float, float, float] | None:
    """Quaternion carrying the camera basis into world axes.

    Prefers the native ``FreeCAD.Rotation`` three-vector constructor and
    falls back to the pure-Python basis conversion so the derivation stays
    deterministic where the native constructor is unavailable or returns
    an unusable quaternion. FreeCAD's ``Rotation.Q`` order is
    ``(x, y, z, w)``.
    """

    import FreeCAD

    rotation = getattr(FreeCAD, "Rotation", None)
    vector = getattr(FreeCAD, "Vector", None)
    if rotation is not None and vector is not None:
        try:
            native = rotation(vector(*x), vector(*y), vector(*z))
            qx, qy, qz, qw = (float(value) for value in native.Q)
            quat = (qx, qy, qz, qw)
            norm = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
            if math.isfinite(norm) and norm > 0.0:
                return quat
        except Exception:
            pass
    return _quat_from_basis(x, y, z)


def _derived_camera_string(
    camera: str,
    center: tuple[float, float, float],
    direction: tuple[float, float, float],
    up: tuple[float, float, float],
    fallback_distance: float | None = None,
) -> str | None:
    """Rewrite ``position``/``orientation`` in a post-framing camera string.

    The camera is placed at ``center - direction * distance`` looking along
    ``direction`` with screen ``up``, keeping the framing distance of the
    given camera (its parsed ``focalDistance``) or ``fallback_distance``.
    The orientation is written in the axis-angle form the Inventor camera
    format uses (a quaternion is converted, never written raw).
    Returns ``None`` when the string lacks a parseable position/orientation
    or no distance is available; the caller refuses the derivation instead
    of capturing a mis-oriented panel. All other camera fields (projection,
    aspect, near/far planes) are preserved untouched.
    """

    parsed = _parse_camera(camera)
    if parsed is None:
        return None
    distance = parsed["focalDistance"][0][0] if "focalDistance" in parsed else fallback_distance
    if distance is None or not math.isfinite(distance) or distance <= 0.0:
        return None
    z_axis = _vec_negate(direction)
    y_axis = up
    x_axis = _vec_cross(y_axis, z_axis)
    x_axis = _vec_normalize(x_axis)
    y_axis = _vec_normalize(y_axis)
    z_axis = _vec_normalize(z_axis)
    if x_axis is None or y_axis is None or z_axis is None:
        return None
    quat = _rotation_quat(x_axis, y_axis, z_axis)
    if quat is None:
        return None
    position = _vec_sub(center, _vec_scale(direction, distance))
    axis, angle = _quat_to_axis_angle(quat)
    result = camera
    # Splices are applied from the highest character offset first so the
    # earlier span stays valid; everything outside the two value runs
    # stays byte-identical, as the native setCamera requires.
    splices = sorted(
        [
            (
                parsed["position"][1],
                parsed["position"][2],
                " ".join(f"{value:.9g}" for value in position),
            ),
            (
                parsed["orientation"][1],
                parsed["orientation"][2],
                " ".join(f"{value:.9g}" for value in (*axis, angle)),
            ),
        ],
        reverse=True,
    )
    for start, end, replacement in splices:
        result = result[:start] + replacement + result[end:]
    return result


def _vector3(value: Any) -> tuple[float, float, float] | None:
    """Read a Vector-like value as three finite floats."""

    try:
        coords = (value.x, value.y, value.z)
    except AttributeError:
        return None
    if not _finite_vec(coords):
        return None
    return (float(coords[0]), float(coords[1]), float(coords[2]))


def _least_parallel_basis_axis(
    direction: tuple[float, float, float],
) -> tuple[tuple[float, float, float], int]:
    """Document basis axis least parallel to ``direction`` (tie X>Y>Z).

    Used for fit section normals (the plane then contains the direction)
    and for derived detail screen-up.
    """

    best_index = 0
    best_parallel: float | None = None
    for index, basis in enumerate(_BASIS_AXES):
        parallel = abs(_vec_dot(direction, basis))
        if best_parallel is None or parallel < best_parallel:
            best_parallel = parallel
            best_index = index
    return _BASIS_AXES[best_index], best_index


def _projected_screen_up(
    direction: tuple[float, float, float],
) -> tuple[float, float, float] | None:
    """Screen-up for a derived camera: the least parallel basis axis
    projected into the plane perpendicular to ``direction``."""

    axis, _index = _least_parallel_basis_axis(direction)
    projected = _vec_sub(axis, _vec_scale(direction, _vec_dot(axis, direction)))
    return _vec_normalize(projected)
