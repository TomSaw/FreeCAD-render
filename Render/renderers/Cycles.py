# ***************************************************************************
# *                                                                         *
# *   Copyright (c) 2019 Yorik van Havre <yorik@uncreated.net>              *
# *   Copyright (c) 2024 Howefuft <howetuft-at-gmail>                       *
# *                                                                         *
# *   This program is free software; you can redistribute it and/or modify  *
# *   it under the terms of the GNU Lesser General Public License (LGPL)    *
# *   as published by the Free Software Foundation; either version 2 of     *
# *   the License, or (at your option) any later version.                   *
# *   for detail see the LICENCE text file.                                 *
# *                                                                         *
# *   This program is distributed in the hope that it will be useful,       *
# *   but WITHOUT ANY WARRANTY; without even the implied warranty of        *
# *   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the         *
# *   GNU Library General Public License for more details.                  *
# *                                                                         *
# *   You should have received a copy of the GNU Library General Public     *
# *   License along with this program; if not, write to the Free Software   *
# *   Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  *
# *   USA                                                                   *
# *                                                                         *
# ***************************************************************************

"""Cycles renderer plugin for FreeCAD Render workbench."""

# Suggested documentation links:
# NOTE Standalone Cycles is experimental, so no documentation is available.
# Instead, documentation must be searched directly in code (via reverse
# engineering), and in the examples provided with it.
# Here are some links:
# https://wiki.blender.org/wiki/Source/Render/Cycles/Standalone
# https://developer.blender.org/diffusion/C/browse/master/src/
# https://developer.blender.org/diffusion/C/browse/master/src/render/nodes.cpp
# https://developer.blender.org/diffusion/C/browse/master/src/app/cycles_xml.cpp
# https://developer.blender.org/diffusion/C/browse/master/examples/
#
# A few hints (my understanding of cycles_standalone):
#
# The 'int main()' is in 'src/app/cycles_standalone.cpp' (but you may not be
# most interested in it)
#
# The xml input file is processed by 'src/app/cycles_xml.cpp' functions.
# In particular, 'transform' and 'state' nodes are in 'src/app/cycles_xml.cpp'
#
# The entry point is 'xml_read_file', which cascades to 'xml_read_scene' via
# 'xml_read_include' function.
#
# 'xml_read_scene' is a key function to study: it recognizes and dispatches all
# the possible nodes to 'xml_read_*' node-specialized parsing functions.
# A few more 'xml_read_*' (including 'xml_read_node' are defined in
# /src/graph/node_xml.cpp
#
# Most of the other nodes are in 'src/scene' directory

# To activate logging, use this prefix:
# env GLOG_logtostderr=1 GLOG_v=10
#
# https://github.com/google/glog#verbose-logging

# Coordinate system:
#
# FreeCAD (z is up):         Cycles (y is up):
#
#
#  z  y                         y  z
#  | /                          | /
#  .--x                         .--x


import pathlib
import functools
import itertools as it
from math import degrees, asin, sqrt, radians, atan2, acos
import xml.etree.ElementTree as et

import FreeCAD as App

from .utils.sunlight import sunlight

TEMPLATE_FILTER = "Cycles templates (cycles_*.xml)"

DISNEY_IOR = 1.5  # As defined in 's2012_pbs_disney_brdf_notes_v3.pdf'


# ===========================================================================
#                             Objects
# ===========================================================================


def write_mesh(name, mesh, material, **kwargs):
    """Compute a string in renderer SDL to represent a FreeCAD mesh."""
    # Get specific parameters
    cast_caustics = kwargs.get("ObjectCastCaustics", False)
    receive_caustics = kwargs.get("ObjectReceiveCaustics", False)

    # Compute material values
    matval = material.get_material_values(
        name,
        _write_texture,
        _write_value,
        _write_texref,
        kwargs["project_directory"],
    )

    snippet_mat = _write_material(name, matval)

    # Get mesh file
    cyclesfile = mesh.write_file(name, mesh.ExportType.CYCLES)

    # Compute transformation
    trans = [
        " ".join(str(v) for v in col)
        for col in mesh.transformation.get_matrix_columns()
    ]
    trans = "  ".join(trans)

    interpolation = "smooth" if mesh.has_vnormals() else "flat"

    # Caustics
    if cast_caustics or receive_caustics:
        snippet_state = f"""
<object
    name="{name}"
    is_caustics_caster="{cast_caustics}"
    is_caustics_receiver="{receive_caustics}"
/>
<state interpolation="{interpolation}" shader="{name}" object="{name}">"""
    else:
        snippet_state = f"""
<state interpolation="{interpolation}" shader="{name}">"""

    snippet_obj = f"""
    <transform matrix="{trans}">
        <include src="{cyclesfile}" />
    </transform>
</state>
"""

    snippet = snippet_mat + snippet_state + snippet_obj

    return snippet


def write_camera(name, pos, updir, target, fov, resolution, **kwargs):
    """Compute a string in renderer SDL to represent a camera."""

    width, height = resolution

    # Cam rotation is angle(deg) axisx axisy axisz
    # Scale needs to have z inverted to behave like a decent camera.
    # No idea what they have been doing at Blender :)
    snippet = f"""
<!-- Generated by FreeCAD - Camera '{name}' -->
<transform
    rotate="{_write_rotation(pos.Rotation)}"
    translate="{_write_vec(pos.Base)}"
    scale="1 1 -1" >
    <camera
        type="perspective"
        fov="{_write_float(radians(fov))}"
        full_width="{width}"
        full_height="{height}"
    />
</transform>"""

    return snippet


def write_pointlight(name, pos, color, power, **kwargs):
    """Compute a string in renderer SDL to represent a point light."""
    # Get specific parameters
    use_caustics = kwargs.get("LightUseCaustics", False)

    snippet = f"""
<!-- Generated by FreeCAD - Pointlight '{name}' -->
<shader name="{name}_shader">
<emission
    name="{name}_emit"
    color="{_write_color(color)}"
    strength="{_write_float(power * 100)}"
/>
<connect from="{name}_emit emission" to="output surface"/>
</shader>
<state shader="{name}_shader">
<light
    name="{name}"
    light_type="point"
    strength="1 1 1"
    tfm="1 0 0 {pos[0]}  0 1 0 {pos[1]}  0 0 1 {pos[2]}"
    use_caustics="{use_caustics}"
/>
</state>
"""

    return snippet


# TODO Move
def _write_tfm(placement):
    """Translate a FreeCAD placement into a Cycles transformation (string)."""
    mat = placement.Matrix.A
    return " ".join(
        [str(_rnd(i)) for i in it.chain(mat[0:4], mat[4:8], mat[8:12])]
    )


def write_arealight(
    name, pos, size_u, size_v, color, power, transparent, **kwargs
):
    """Compute a string in renderer SDL to represent an area light."""
    strength = power / 100

    use_camera = "false" if transparent else "true"
    snippet = f"""
<!-- Area light '{name}' -->
<shader name="{name}_shader" use_mis="true">
<emission
    name="{name}_emit"
    color="{_write_color(color)}"
    strength="{_write_float(strength)}"
/>
<connect from="{name}_emit emission" to="output surface"/>
</shader>
<state shader="{name}_shader">
<light
    light_type="area"
    strength="1 1 1"
    tfm="{_write_tfm(pos)}"
    sizeu="{_write_float(size_u)}"
    sizev="{_write_float(size_v)}"
    size="1.0"
    round="false"
    use_mis="true"
    use_camera="{use_camera}"
/>
<light
    light_type="area"
    co="{_write_point(pos.Base)}"
    strength="1 1 1"
    tfm="{_write_tfm(pos)}"
    sizeu="{_write_float(size_u)}"
    sizev="{_write_float(size_v)}"
    size="1.0"
    round="false"
    use_mis="true"
    use_camera="{use_camera}"
/>
</state>"""

    return snippet


def write_sunskylight(
    name,
    direction,
    distance,
    turbidity,
    albedo,
    sun_intensity,
    sky_intensity,
    **kwargs,
):
    """Compute a string in renderer SDL to represent a sunsky light."""
    model = kwargs.get("Model", "Hosek-Wilkie")
    use_caustics = kwargs.get("LightUseCaustics", False)

    if model == "Nishita":
        sky_sub = _write_sunskylight_nishita
    elif model == "Hosek-Wilkie":
        sky_sub = _write_sunskylight_hosekwilkie
    else:
        raise NotImplementedError(model)
    return sky_sub(
        name,
        direction,
        turbidity,
        albedo,
        sun_intensity,
        sky_intensity,
        use_caustics,
    )


def _write_sunskylight_hosekwilkie(
    name,
    direction,
    turbidity,
    albedo,
    sun_intensity,
    sky_intensity,
    use_caustics,
):
    """Compute a string in renderer SDL to represent a sunsky light."""
    # We model sun_sky with a sun light and a sky texture for world

    # For sky texture, direction must be normalized
    assert direction.Length
    _dir = App.Vector(direction)
    _dir.normalize()
    theta = acos(_dir.z / sqrt(_dir.x**2 + _dir.y**2 + _dir.z**2))
    sun = sunlight(theta, turbidity)
    rgb = sun.xyz.to_srgb_with_fixed_luminance(1.0)

    # Strength for sun. Should be 1.0, but everything is burnt
    sun_strength = 0.01 * sun_intensity
    sky_strength = 5.0 * sky_intensity
    # Sun angle as seen from earth: 0.5°
    angle = radians(0.5)

    snippet_sky = f"""
<!-- Generated by FreeCAD - Sun_sky light '{name}' -->
<shader name="{name}_bg_shader">
    <background name="{name}_bg" strength="{sky_strength}"/>
    <connect from="{name}_bg background" to="output surface" />
    <sky_texture
        name="{name}_tex"
        sky_type="hosek_wilkie"
        turbidity="{turbidity}"
        sun_direction="{_dir.x}, {_dir.y}, {_dir.z}"
        ground_albedo="{albedo}"
    />
    <connect from="{name}_tex color" to="{name}_bg color" />
</shader>
<background shader="{name}_bg_shader" />
"""
    snippet_sun = f"""\
<shader name="{name}_shader">
    <emission name="{name}_emit"
        color="{rgb[0]} {rgb[1]} {rgb[2]}"
        strength="{sun.irradiance}"
    />
    <connect from="{name}_emit emission" to="output surface"/>
</shader>
<state shader="{name}_shader">
    <light
        light_type="distant"
        use_mis="true"
        strength="{sun_strength} {sun_strength} {sun_strength}"
        tfm="{_write_tfm(_dir2plc(-direction))}"
        angle="{angle}"
        use_caustics="{use_caustics}"
    />
</state>
"""
    return "".join([snippet_sky, snippet_sun])


def _write_sunskylight_nishita(
    name,
    direction,
    turbidity,
    albedo,
    sun_intensity,
    sky_intensity,
    use_caustics,
):
    """Compute a string in renderer SDL to represent a sunsky light."""
    # We use the new improved nishita model (2020)

    assert direction.Length
    _dir = App.Vector(direction)
    _dir.normalize()
    theta = asin(_dir.z / sqrt(_dir.x**2 + _dir.y**2 + _dir.z**2))
    phi = atan2(_dir.x, _dir.y)

    snippet_shader = f"""
<!-- Generated by FreeCAD - Sun_sky light '{name}' -->
<shader name="{name}_shader" use_mis="true">
    <sky_texture
        name="{name}_tex"
        sky_type="nishita_improved"
        turbidity="{_write_float(turbidity)}"
        ground_albedo="{_write_float(albedo)}"
        sun_disc="true"
        sun_elevation="{_write_float(theta)}"
        sun_rotation="{_write_float(phi)}"
        sun_size="{radians(0.545)}"
        sun_intensity="{sun_intensity}"
        altitude="500"
    />
    <emission
        name="{name}_emit"
        strength="0.2"
    />
    <connect from="{name}_tex color" to="{name}_emit color" />
    <connect from="{name}_emit emission" to="output surface" />
</shader>"""

    sky_strength = 0.2 * sky_intensity

    snippet_sun = f"""
<state shader="{name}_shader">
<light
    light_type="background"
    strength="{sky_strength} {sky_strength} {sky_strength}"
    use_mis="true"
    use_caustics="{use_caustics}"
/>
</state>"""

    snippet_sky = f"""
<background shader="{name}_shader"/>
"""

    return "".join([snippet_shader, snippet_sun, snippet_sky])


def write_imagelight(name, image, **_):
    """Compute a string in renderer SDL to represent an image-based light."""
    # Caveat: Cycles requires the image file to be in the same directory
    # as the input file
    filename = pathlib.Path(image).name
    snippet = f"""
<!-- Generated by FreeCAD - Image-based light '{name}' -->
<background>
    <background name="{name}_bg" />
    <environment_texture
        name= "{name}_tex"
        filename = "{filename}"
        colorspace = "__builtin_raw"
    />
    <connect from="{name}_tex color" to="{name}_bg color" />
    <connect from="{name}_bg background" to="output surface" />
</background>
"""
    return snippet


def write_distantlight(
    name,
    color,
    power,
    direction,
    angle,
    **kwargs,
):
    """Compute a string in renderer SDL to represent a distant light."""
    # Get specific parameters
    use_caustics = kwargs.get("LightUseCaustics", False)

    strength = _write_float(power)
    # For Cycles, angle must be in radians, so we have to convert
    angle = radians(angle)
    angle = _write_float(angle)

    snippet = f"""
<!-- Generated by FreeCAD - Distant light '{name}' -->
<shader name="{name}_shader">
<emission
    name="{name}_emit"
    color="{_write_color(color)}"
    strength="{strength} {strength} {strength}"
/>
<connect from="{name}_emit emission" to="output surface"/>
</shader>
<state shader="{name}_shader">
<light
        name="{name}"
        light_type="distant"
        strength="1 1 1"
        angle="{angle}"
        tfm="{_write_tfm(_dir2plc(direction))}"
        use_caustics="{use_caustics}"
/>
</state>
"""
    return snippet


# ===========================================================================
#                              Material implementation
# ===========================================================================


def _write_material(name, matval):
    """Compute a string in the renderer SDL, to represent a material.

    This function should never fail: if the material is not recognized,
    a fallback material is provided.
    """
    # Bsdf
    shadertype = matval.shadertype
    try:
        material_function = MATERIALS[shadertype]
    except KeyError:
        # Unknown shader - fallback
        msg = (
            "'{}' - Material '{}' unknown by renderer, using fallback "
            "material\n"
        )
        App.Console.PrintWarning(msg.format(name, shadertype))
        snippet_mat = _write_material_fallback(name, matval)
        return f"""
<!-- Generated by FreeCAD - Shader 'Fallback' - Object '{name}' -->
<shader name="{name}">
{snippet_mat}
</shader>
"""

    # Get material snippet
    snippet_mat = material_function(name, matval)

    # Textures
    snippet_tex = matval.write_textures()

    # Add bump node (for bump and normal...) to textures
    # if necessary...
    if matval.has_bump() or matval.has_normal():
        bump_factor = matval.get_bump_factor()
        bump_snippet = f"""
<bump
    name="{name}_bump"
    use_object_space = "false"
    invert = "false"
    distance = "{bump_factor}"
    strength = "0.2"
/>
<connect from="{name}_bump normal" to="{name}_bsdf normal"/>"""

        snippet_tex = f"""\
{bump_snippet}
{snippet_tex}"""

    # Final result
    snippet_shader = f"""
<!-- Generated by FreeCAD - Shader '{shadertype}' - Object '{name}' -->
<shader name="{name}">
{snippet_mat}
{snippet_tex}
</shader>
"""

    return snippet_shader


def _write_material_passthrough(name, matval):
    """Compute a string in the renderer SDL for a passthrough material."""
    texture = matval.passthrough_texture
    snippet = matval["string"]
    return snippet.format(
        n=name, c=matval.default_color.to_linear(), tex=texture
    )


def _write_material_glass(name, matval, connect_to="output surface"):
    """Compute a string in the renderer SDL for a glass material."""
    return f"""
<glass_bsdf
    name="{name}_bsdf"
    IOR="{matval["ior"]}"
    color="{matval["color"]}"
/>
<connect from="{name}_bsdf bsdf" to="{connect_to}"/>"""


def _write_material_disney(name, matval, connect_to="output surface"):
    """Compute a string in the renderer SDL for a Disney material."""
    # For ascending compatibility reasons, we kept sheen, clearcoat
    # and clearcoat_roughness
    sheentint = matval["sheentint"]
    return f"""
<principled_bsdf
    name="{name}_bsdf"
    base_color = "{matval["basecolor"]}"
    subsurface_weight = "{matval["subsurface"]}"
    metallic = "{matval["metallic"]}"
    ior = "{DISNEY_IOR}"
    specular_ior_level = "{matval["specular"]}"
    specular_tint = "{matval["speculartint"]}"
    roughness = "{matval["roughness"]}"
    anisotropic = "{matval["anisotropic"]}"
    sheen_weight = "{matval["sheen"]}"
    sheen_tint = "{sheentint}"
    coat_weight = "{matval["clearcoat"]}"
    coat_ior = "{DISNEY_IOR}"
/>
<math
    name="{name}_clearcoatgloss_invert"
    math_type="subtract"
    value1="1.0"
    value2="{matval["clearcoatgloss"]}"
/>
<connect
    from="{name}_clearcoatgloss_invert value"
    to="{name}_bsdf clearcoat_roughness"
/>
<connect
    from="{name}_clearcoatgloss_invert value"
    to="{name}_bsdf coat_roughness"
/>
<mix
    name="{name}_sheentint"
    mix_type="mix"
    color1="1.0 1.0 1.0"
    color2="{matval["basecolor"]}"
    fac="{matval["sheentint"]}"
/>
<connect
    from="{name}_sheentint color"
    to="{name}_bsdf sheen_tint"
/>
<connect from="{name}_bsdf bsdf" to="{connect_to}"/>"""


def _write_material_pbr(name, matval, connect_to="output surface"):
    """Compute a string in the renderer SDL for a Disney material."""
    return f"""
<principled_bsdf
    name="{name}_bsdf"
    base_color = "{matval["basecolor"]}"
    roughness = "{matval["roughness"]}"
    metallic = "{matval["metallic"]}"
    specular = "{matval["metallic"]}"
/>
<connect from="{name}_bsdf bsdf" to="{connect_to}"/>"""


def _write_material_diffuse(name, matval, connect_to="output surface"):
    """Compute a string in the renderer SDL for a Diffuse material."""
    return f"""
<diffuse_bsdf name="{name}_bsdf" color="{matval["color"]}"/>
<connect from="{name}_bsdf bsdf" to="{connect_to}"/>"""


def _write_material_mixed(name, matval, connect_to="output surface"):
    """Compute a string in the renderer SDL for a Mixed material."""
    # Glass
    matval.objname = f"{name}_glass"  # Ugly workaround
    submatval_g = matval.getmixedsubmat(submat="glass")
    snippet_g = _write_material_glass(
        f"{name}_glass", submatval_g, f"{name}_bsdf closure2"
    )
    snippet_g_tex = submatval_g.write_textures()

    # Diffuse
    matval.objname = f"{name}_diffuse"  # Ugly workaround
    submatval_d = matval.getmixedsubmat(submat="diffuse")
    snippet_d = _write_material_diffuse(
        f"{name}_diffuse", submatval_d, f"{name}_bsdf closure1"
    )
    snippet_d_tex = submatval_d.write_textures()

    # Mix
    snippet_m = f"""
<mix_closure name="{name}_bsdf" fac="{matval["transparency"]}" />
<connect from="{name}_bsdf closure" to="{connect_to}" />"""

    return snippet_m + snippet_g + snippet_d + snippet_g_tex + snippet_d_tex


def _write_material_carpaint(name, matval, connect_to="output surface"):
    """Compute a string in the renderer SDL for a carpaint material."""
    return f"""
<!-- Main: principled with coating -->
<principled_bsdf
    name="{name}_bsdf"
    base_color = "{matval["basecolor"]}"
    specular = "0.1"
    roughness = "0.5"
    coat = "1.0"
    coat_roughness = "0.05"
/>
<connect from="{name}_bsdf bsdf" to="{connect_to}"/>

<!-- ColorRamp for noise -->
<rgb_ramp
    name="{name}_noiseramp"
    ramp="1.0 1.0 1.0 0.0 0.0 0.0"
    ramp_alpha="0.0 0.4"
/>
<connect from="{name}_noiseramp color" to="{name}_bsdf metallic"/>

<!-- Noise -->
<noise_texture
    name="{name}_noise"
    dimensions="3D"
    scale="1000000"
    detail="5"
/>
<connect from="{name}_noise fac" to="{name}_noiseramp fac"/>"""


def _write_material_fallback(name, matval):
    """Compute a string in the renderer SDL for a fallback material.

    Fallback material is a simple Diffuse material.
    """
    try:
        lcol = matval.default_color.to_linear()
        red = float(lcol[0])
        grn = float(lcol[1])
        blu = float(lcol[2])
        assert (0 <= red <= 1) and (0 <= grn <= 1) and (0 <= blu <= 1)
    except (AttributeError, ValueError, TypeError, AssertionError):
        red, grn, blu = 1, 1, 1
    snippet = """
<diffuse_bsdf name="{n}_bsdf" color="{r}, {g}, {b}"/>
<connect from="{n}_bsdf bsdf" to="output surface"/>"""
    return snippet.format(n=name, r=red, g=grn, b=blu)


def _write_material_emission(name, matval, connect_to="output surface"):
    """Compute a string in the renderer SDL for a Emission material."""
    # https://github.com/blender/cycles/blob/ccc73ccc570a92f59b0cd63f56a688a7782e346d/src/scene/shader_nodes.cpp#L3144
    return f"""
<emission
    name="{name}_bsdf"
    color="{matval["color"]}"
    strength="{matval["power"]}"
/>
<connect from="{name}_bsdf emission" to="{connect_to}"/>"""


MATERIALS = {
    "Passthrough": _write_material_passthrough,
    "Glass": _write_material_glass,
    "Disney": _write_material_disney,
    "Diffuse": _write_material_diffuse,
    "Mixed": _write_material_mixed,
    "Carpaint": _write_material_carpaint,
    "Substance_PBR": _write_material_pbr,
    "Emission": _write_material_emission,
}

# ===========================================================================
#                             Textures
# ===========================================================================

# Mapping between shader fields and sockets to connect texture to
SOCKET_MAPPING = {
    "ior": "IOR",
    "basecolor": "base_color",
    "speculartint": "specular_tint",
    "sheentint": "sheen_tint",
    "transparency": "fac",
}


def _write_texture(**kwargs):
    """Compute a string in renderer SDL to describe a texture.

    The texture is computed from a property of a shader (as the texture is
    always integrated into a shader). Property's data are expected as
    arguments.

    Args:
        objname -- Object name for which the texture is computed
        propname -- Name of the shader property
        propvalue -- Value of the shader property

    Returns:
        the name of the texture
        the SDL string of the texture
    """
    # Retrieve parameters
    objname = kwargs["objname"]
    propname = kwargs["propname"]
    propvalue = kwargs["propvalue"]

    # Compute socket name (by default, it should yield propname...)
    socket = SOCKET_MAPPING.get(propname, propname)

    # Compute texture name
    texname = f"{objname}_{propname}_tex"

    # Compute file name
    # Caveat: Cycles requires the image file to be in the same directory
    # as the input file
    filename = pathlib.Path(propvalue.file).name

    scale = float(propvalue.scale)
    rotation = -radians(float(propvalue.rotation))
    translation_u = float(propvalue.translation_u)
    translation_v = float(propvalue.translation_v)

    # https://blender.stackexchange.com/questions/16443/using-a-normal-map-together-with-a-bump-map

    if propname == "bump":
        colorspace = "__builtin_raw"
        connect = f"""
<connect from="{texname} color" to="{objname}_bump height"/>"""

    elif propname == "normal":
        colorspace = "__builtin_raw"
        normal_strength = propvalue.scalar
        connect = f"""
<normal_map
    name="{texname}_normalmap"
    space="tangent"
    strength="{normal_strength}"
/>
<connect from="{texname} color" to="{texname}_normalmap color"/>
<connect from="{texname}_normalmap normal" to="{objname}_bump normal"/>"""

    elif propname == "displacement":
        colorspace = "__builtin_raw"
        connect = f"""
<normal_map
    name="{texname}_normalmap_disp"
    space="tangent"
    strength="0.2"
/>
<connect from="{texname} color" to="output displacement"/>"""

    elif propname == "subsurface":
        colorspace = "__builtin_raw"
        connect = f"""
<connect
    from="{texname} color"
    to="{objname}_bsdf subsurface_weight"
/>"""

    elif propname == "sheen":
        colorspace = "__builtin_raw"
        connect = f"""
<connect
    from="{texname} color"
    to="{objname}_bsdf sheen_weight"
/>"""

    elif propname == "sheentint":
        colorspace = "__builtin_raw"
        connect = f"""
<connect
    from="{texname} color"
    to="{objname}_sheentint fac"
/>"""

    elif propname == "clearcoat":
        colorspace = "__builtin_raw"
        connect = f"""
<connect
    from="{texname} color"
    to="{objname}_bsdf coat_weight"
/>"""

    elif propname == "clearcoatgloss":
        colorspace = "__builtin_raw"
        connect = f"""
<connect
    from="{texname} color"
    to="{objname}_clearcoatgloss_roughness value2"
/>"""

    else:
        # Plain texture
        colorspace = (
            "__builtin_srgb" if "color" in propname else "__builtin_raw"
        )
        connect = f"""
<connect from="{texname} color" to="{objname}_bsdf {socket}"/>"""

    texture_core = f"""
<image_texture
    name="{texname}"
    filename="{filename}"
    colorspace="{colorspace}"
    tex_mapping.scale="{scale} {scale} {scale}"
    tex_mapping.rotation="0 0 {rotation}"
    tex_mapping.translation="{translation_u} {translation_v} 0.0"
/>"""

    return texname, texture_core + connect


def _write_value(**kwargs):
    """Compute a string in renderer SDL from a shader property value.

    Args:
        proptype -- Shader property's type
        propvalue -- Shader property's value

    The result depends on the type of the value...
    """
    # Retrieve parameters
    proptype = kwargs["proptype"]
    val = kwargs["propvalue"]

    # Snippets for values
    if proptype == "RGB":
        lcol = val.to_linear(precise=True)
        value = f"{_rnd(lcol[0])} {_rnd(lcol[1])} {_rnd(lcol[2])}"
    elif proptype == "float":
        value = f"{_rnd(val)}"
    elif proptype == "node":
        value = ""
    elif proptype == "RGBA":
        lcol = val.to_linear()
        value = (
            f"{_rnd(lcol[0])} {_rnd(lcol[1])} {_rnd(lcol[2])} {_rnd(lcol[3])}"
        )
    elif proptype == "texonly":
        value = f"{val}"
    elif proptype == "str":
        value = f"{val}"
    else:
        raise NotImplementedError

    return value


def _write_texref(**kwargs):  # pylint: disable=unused-argument
    """Compute a string in SDL for a reference to a texture in a shader."""
    return "0.0"  # In Cycles, there is no reference to textures in shaders...


# ===========================================================================
#                              Helpers
# ===========================================================================


_rnd = functools.partial(round, ndigits=8)  # Round to 8 digits (helper)

_write_float = _rnd


def _write_point(pnt):
    """Write a point."""
    return f"{_rnd(pnt[0])} {_rnd(pnt[1])} {_rnd(pnt[2])}"


_write_vec = _write_point  # Write a vector


def _write_rotation(rot):
    """Write a rotation."""
    return f"{_rnd(degrees(rot.Angle))} {_write_vec(rot.Axis)}"


def _write_color(col):
    """Write a color.

    Args:
        col -- a utils.RGB color"""
    lcol = col.to_linear()
    return f"{_rnd(lcol[0])} {_rnd(lcol[1])} {_rnd(lcol[2])}"


NULLVEC = App.Vector(0.0, 0.0, 0.0)
ZVEC = App.Vector(0.0, 0.0, 1.0)


def _dir2plc(direction):
    """Compute a placement that transforms (0,0,-1) into 'direction'.

    Args:
        direction -- The target direction (3D vector)

    Returns a FreeCAD placement.
    """
    z_out = App.Vector(-direction)
    x_out = ZVEC.cross(z_out)
    if x_out.isEqual(NULLVEC, 1e-6):
        # z_in and z_out are colinear:
        # Placement is identity
        return App.Placement()
    y_out = z_out.cross(x_out)
    return App.Placement(App.Matrix(x_out, y_out, z_out))


# ===========================================================================
#                              Test function
# ===========================================================================


def test_cmdline(_):
    """Generate a command line for test.

    This function allows to test if renderer settings (path...) are correct
    """
    params = App.ParamGet("User parameter:BaseApp/Preferences/Mod/Render")
    rpath = params.GetString("CyclesPath", "")
    return [rpath, "--help"]


# ===========================================================================
#                              Render function
# ===========================================================================


def render(
    project,
    prefix,
    batch,
    input_file,
    output_file,
    width,
    height,
    spp,
    denoise,
):
    """Generate renderer command.

    Args:
        project -- The project to render
        prefix -- A prefix string for call (will be inserted before path to
            renderer)
        batch -- A boolean indicating whether to call UI (false) or console
            (true) version of renderer
        input_file -- path to input file
        output -- path to output file
        width -- Rendered image width, in pixels
        height -- Rendered image height, in pixels
        spp -- Max samples per pixel (halt condition)
        denoise -- Flag to run denoiser

    Returns:
        The command to run renderer (string)
        A path to output image file (string)
    """

    def enclose_rpath(rpath):
        """Enclose rpath in quotes, if needed."""
        if not rpath:
            return ""
        if rpath[0] == rpath[-1] == '"':
            # Already enclosed (double quotes)
            return rpath
        if rpath[0] == rpath[-1] == "'":
            # Already enclosed (simple quotes)
            return rpath
        return f'"{rpath}"'

    # Denoise
    if denoise:
        tree = et.parse(input_file)
        root = tree.getroot()
        integrator = root.find("integrator")
        if integrator is None:
            integrator = et.Element("integrator")
            root.append(integrator)
        integrator.set("use_denoise", "true")
        tree.write(input_file, encoding="unicode")

    # Prepare command line arguments
    params = App.ParamGet("User parameter:BaseApp/Preferences/Mod/Render")
    prefix = params.GetString("Prefix", "")
    if prefix:
        prefix += " "
    rpath = params.GetString("CyclesPath", "")
    args = params.GetString("CyclesParameters", "")
    args += f""" --output "{output_file}" """
    if batch:
        args += " --background"
    if spp:
        args += f" --samples {spp}"
    if not rpath:
        App.Console.PrintError(
            "Unable to locate renderer executable. "
            "Please set the correct path in "
            "Edit -> Preferences -> Render\n"
        )
        return None, None
    rpath = enclose_rpath(rpath)
    args += " --width " + str(width)
    args += " --height " + str(height)
    filepath = f'"{input_file}"'
    cmd = prefix + rpath + " " + args + " " + filepath

    return cmd, output_file
