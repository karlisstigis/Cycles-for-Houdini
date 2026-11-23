// SPDX-License-Identifier: Apache-2.0
// Native VOP authoring through public HDK interfaces. Network evaluation is
// performed by our owned graph adapter, without VEX code generation.
#include "cycles_houdini/shader.h"
#include "cycles_houdini/session.h"
#include "material_subnet.h"
#include "material_vop.h"
#include "material_vop_ui.h"
#include "material_preview.h"
#include "scene_persistence.h"
#include <PRM/PRM_SpareData.h>
#include <PRM/PRM_Conditional.h>
#include <PY/PY_Python.h>
#include <PY/PY_InterpreterAutoLock.h>
#include <PXL/PXL_OCIO.h>
#include <UT/UT_StringArray.h>
#include <UT/UT_IStream.h>
#include <UT/UT_WorkBuffer.h>
#include <algorithm>
#include <cctype>
#include <cstring>
#include <array>
#include <deque>
#include <map>
#include <memory>
#include <numbers>
#include <iomanip>
#include <random>
#include <sstream>
#include <set>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <tuple>
#include <vector>
#include <OP/OP_OperatorTable.h>
#include <OP/OP_Input.h>
#include <CH/CH_Manager.h>
#include <PRM/PRM_Include.h>
#include <UT/UT_String.h>
#include <VOP/VOP_Node.h>
#include <VOP/VOP_Operator.h>
#include <VOP/VOP_TypeInfo.h>

namespace ch = cycles_houdini;
void ch::set_native_shader_identity(OP_Node &node) {
  std::random_device random;
  std::ostringstream id;
  id << std::hex << std::setfill('0');
  for (int i = 0; i < 4; ++i)
    id << std::setw(8) << random();
  node.setUserData("cycles_graph_id", UT_StringHolder(id.str()), false);
}
namespace {
void texture_colorspace_menu(void *, PRM_Name *names, int size, const PRM_SpareData *, const PRM_Parm *) {
  if (size < 1)
    return;
  int index = 0;
  const auto append = [&](const char *token, const char *label) {
    if (index < size - 1)
      names[index++] = PRM_Name(PRM_Name::COPY, UT_StringHolder(token), UT_StringHolder(label));
  };
  append("auto", "Auto from Houdini OCIO and file");
  append("data", "Data (no color conversion)");
  append("scene_linear", "Render working space");
  UT_StringArray spaces;
  PXL_OCIO::getColorSpaces(spaces);
  for (const auto &name : spaces)
    if (name != "auto" && name != "data" && name != "scene_linear")
      append(name.c_str(), name.c_str());
  names[index].setAsSentinel();
}
struct Metadata {
  const char *label, *category, *help;
};
const std::map<std::string, Metadata> metadata = {
    {"enable_output_float",
     {"Enable Output (Float)", "Converter",
      "Disabled outputs produce zero and prune the value branch. Varying Enable uses Blender 5.3's clamped Mix "
      "lowering."}},
    {"enable_output_integer",
     {"Enable Output (Integer)", "Converter",
      "Disabled outputs produce zero. Constant Enable preserves int32 precision; varying Enable uses Blender 5.3's "
      "float Mix path."}},
    {"enable_output_boolean",
     {"Enable Output (Boolean)", "Converter",
      "Disabled outputs produce false and prune value dependencies. Varying Enable follows Blender 5.3."}},
    {"enable_output_vector",
     {"Enable Output (Vector)", "Vector",
      "Disabled outputs produce the zero vector and prune value dependencies. Varying Enable uses clamped Mix."}},
    {"enable_output_color",
     {"Enable Output (Color)", "Color",
      "Disabled outputs produce black and prune value dependencies. Varying Enable uses clamped Mix."}},
    {"enable_output_shader",
     {"Enable Output (Shader)", "Shader",
      "Disabled outputs produce an empty black shader and prune dependencies. Varying Enable uses Mix Shader."}},
    {"warning",
     {"Warning", "Output",
      "Report an owned message when constant Show is true, including when this node's output is unconnected. Constant "
      "false skips the message. Varying Show produces an information diagnostic and false output, matching Blender's "
      "constant-only warning behavior."}},
    {"switch_float",
     {"Switch (Float)", "Converter",
      "Select False or True. Constant selectors prune the unused branch; varying selectors use Blender 5.3's "
      "greater-than-zero and clamped Mix lowering."}},
    {"switch_integer",
     {"Switch (Integer)", "Converter",
      "Select integer branches. Constant selection preserves int32 precision. Varying selection follows Blender 5.3's "
      "float Mix path and its float precision limit."}},
    {"switch_boolean",
     {"Switch (Boolean)", "Converter", "Select Boolean branches with the Blender 5.3 switch lowering."}},
    {"switch_vector",
     {"Switch (Vector)", "Vector",
      "Select vector branches. Constant selection prunes inactive dependencies; varying selection uses Blender 5.3's "
      "clamped Mix."}},
    {"switch_color",
     {"Switch (Color)", "Color",
      "Select color branches in the render working space using Blender 5.3 switch semantics."}},
    {"switch_shader",
     {"Switch (Shader)", "Shader",
      "Select surface/volume shader branches. Varying selection uses Mix Shader with a zero-or-one factor."}},
    {"index_switch_float",
     {"Index Switch (Float)", "Converter",
      "Zero-based multiparm choices. Out-of-range indices produce zero. Constants prune unused branches; varying "
      "indices truncate and use Blender 5.3's clamped Mix chain. Existing shader graph budgets still apply."}},
    {"index_switch_integer",
     {"Index Switch (Integer)", "Converter",
      "Zero-based integer choices. Constant selection preserves int32 precision; varying selection follows Blender "
      "5.3's float Mix path. Out-of-range indices produce zero."}},
    {"index_switch_boolean",
     {"Index Switch (Boolean)", "Converter",
      "Zero-based Boolean choices; out-of-range indices produce false. Uses Blender 5.3's static pruning or varying "
      "Mix chain."}},
    {"index_switch_vector",
     {"Index Switch (Vector)", "Vector",
      "Zero-based vector choices; out-of-range indices produce the zero vector. Uses Blender 5.3's static pruning or "
      "varying Mix chain."}},
    {"index_switch_color",
     {"Index Switch (Color)", "Color",
      "Zero-based color choices; out-of-range indices produce black. Uses Blender 5.3's static pruning or varying Mix "
      "chain."}},
    {"index_switch_shader",
     {"Index Switch (Shader)", "Shader",
      "Zero-based shader choices; out-of-range indices produce an empty black shader. Uses Blender 5.3's static "
      "pruning or varying Mix Shader chain."}},
    {"boolean_math",
     {"Boolean Math", "Converter",
      "Integer Boolean operations from Cycles. Values greater than zero are true; zero and negative integers are "
      "false. Results are integer 0 or 1. Float links convert using Cycles integer conversion before Boolean "
      "evaluation."}},
    {"integer_math",
     {"Integer Math", "Converter",
      "Native Cycles signed 32-bit integer arithmetic, including truncated/floor/ceiling/rounded division, modulo, GCD "
      "and LCM. Inputs, links and results retain integer precision; operation edge cases follow the pinned engine."}},
    {"get_vector_component",
     {"Get Vector Component", "Converter",
      "Select a vector component by integer index using the pinned Cycles operation. Index conversion and out-of-range "
      "behavior follow the engine."}},
    {"background_shader",
     {"Background", "Shader",
      "World radiance, scaled by Strength. Connect to Cycles World Output. Color uses the render working space; a "
      "display transform does not change illumination."}},
    {"environment_texture",
     {"Environment Texture", "Texture",
      "Equirectangular or mirror-ball environment image. Unconnected Vector uses Cycles' world ray direction with Z "
      "up. Use Mapping for rotation; native Houdini environment lights supply the Y-up conversion. Auto follows "
      "Houdini OCIO and file metadata."}},
    {"sky_texture",
     {"Sky Texture", "Texture",
      "Procedural Cycles sky with Multiple/Single Scattering and legacy Preetham/Hosek-Wilkie models. Native Sky "
      "Rotation defaults to 90 degrees about X so the horizon follows Houdini Y up. Zero rotation gives Cycles Z up. "
      "Sun angles and Sky Rotation are degrees; the owned engine graph stores radians. Leave Vector unconnected for "
      "efficient solar-disc sampling. Altitude is metres above sea level, independent of camera translation. Output "
      "uses the render working space. Modern skies have high physical radiance; use display exposure to inspect "
      "them."}},
    {"aov_output",
     {"AOV Output", "Output",
      "Write Color or Value to a named shader AOV enabled on the Cycles ROP Passes tab. Match the name exactly; the "
      "ROP selects which input is used. No connection to Surface Output is needed. Unregistered outputs are ignored. "
      "Color is scene-linear RGBA; Value is raw scalar data. Names use ASCII letters, digits and underscores, starting "
      "with a letter or underscore."}},
    {"add_closure", {"Add Shader", "Shader", "Add two shaders in the same surface or volume domain."}},
    {"absorption_volume",
     {"Volume Absorption", "Volume",
      "Absorb light per world-space length. Connect a density field explicitly; the node's constant Density does not "
      "read a VDB grid automatically."}},
    {"attribute",
     {"Attribute", "Input",
      "Read named numeric scalar/vector Houdini fields on meshes, points and curves; integers convert to renderer "
      "float precision. Vertex, point, primitive, then detail precedence. Deformation motion samples compatible "
      "numeric fields across the shutter; unavailable samples retain the centre value with a warning. Cd and "
      "point_random/curve_random retain their standard meaning. Volume inputs read named grids. Missing/broken fields "
      "recover locally."}},
    {"color", {"Color", "Input", "A scene-linear RGB constant. Display transforms do not change its rendering value."}},
    {"combine_xyz", {"Combine XYZ", "Converter", "Combine three scalar components into a vector."}},
    {"diffuse_bsdf",
     {"Diffuse BSDF", "Shader", "Cycles diffuse reflection. An unconnected Normal uses the surface shading normal."}},
    {"displacement",
     {"Displacement", "Vector",
      "Normal-directed displacement. Height minus Midlevel is multiplied by Scale. Object space preserves local units; "
      "true/both methods change mesh positions."}},
    {"emission",
     {"Emission", "Shader",
      "Scene-linear emitted radiance is Color multiplied by Strength. HDR values are preserved."}},
    {"fresnel",
     {"Fresnel", "Input",
      "Dielectric Fresnel factor from the incoming ray and Normal. An unconnected Normal uses the surface shading "
      "normal."}},
    {"geometry",
     {"Geometry", "Input",
      "Cycles surface geometry information in engine coordinates. Random Per Island hashes disconnected mesh "
      "components. Pointiness uses original polygon neighborhoods and geometric normals at the requested frame, before "
      "subdivision/displacement; authored N does not define it. Copies share intrinsic values. Points, hair and volume "
      "inputs recover locally for these mesh-only outputs."}},
    {"glass_bsdf",
     {"Glass BSDF", "Shader",
      "Dielectric reflection and refraction with roughness, anisotropy, tangent and rotation. IOR is the material "
      "index of refraction; Nested Dielectrics forms the ratio against the opposite medium. Film transparent-glass "
      "controls affect output coverage; environment illumination remains enabled."}},
    {"holdout",
     {"Holdout", "Shader",
      "Create a matte hole in the surface contribution when transparent film is enabled. Use a surface material; this "
      "adapter does not expose volume holdouts."}},
    {"transparent_bsdf",
     {"Transparent BSDF", "Shader",
      "Transmit a ray without refraction. White transmits fully; colored values attenuate transmitted light and "
      "coverage. Mix with other closures for material opacity."}},
    {"image_texture",
     {"Image Texture", "Texture",
      "Read an image through Cycles' image manager. Unconnected Vector uses Houdini uv. Choose data for normal/height "
      "maps, scene_linear for working-space values, or an OCIO input space for color. UDIM and UVTILE tokens discover "
      "existing tiles; a tile list restricts loading. Frame expressions resolve at the render time."}},
    {"ies_light",
     {"IES Texture", "Texture",
      "Read an IES photometric distribution. Connect Fac to Emission Strength. Vector is the outgoing direction, with "
      "the profile axis along local -Z. Use Vector Transform to convert Geometry Incoming from World to Object for "
      "rotating analytic lights. Values follow the pinned Cycles D65 candela-to-radiometric convention. Missing, "
      "malformed or unsupported tilted profiles disable this modulation with a warning."}},
    {"uvmap",
     {"UV Map", "Input",
      "Read a two/three-component Houdini UV field by name. An empty attribute or uv selects the default map. "
      "Deformation motion samples compatible maps across the shutter; missing maps recover locally. The default hair "
      "map is root-based; named hair maps follow curve keys. Instancer coordinates require a separate adapter."}},
    {"mapping",
     {"Mapping", "Vector",
      "Transform coordinates using Cycles point, texture, vector or normal semantics. Rotation is in radians; point "
      "applies Scale, Rotation then Location."}},
    {"bump",
     {"Bump", "Vector",
      "Evaluate height derivatives in Cycles. Strength and Distance control the perturbed normal; connect Normal to a "
      "surface shader. Use data interpretation for height images."}},
    {"normal_map",
     {"Normal Map", "Vector",
      "Interpret RGB as a normal map. Tangent space accepts the default uv or a named two/three-component UV field on "
      "polygon meshes. Base selects original or displaced geometry; Deformation samples the tangent basis across the "
      "shutter. OpenGL/DirectX controls the green channel. Use data interpretation for the image."}},
    {"noise_texture",
     {"Noise Texture", "Texture",
      "Procedural Cycles Perlin/fractal noise. Unconnected Vector uses world-space position in Houdini units, "
      "including for true displacement. Connect Geometry Position explicitly for the same behavior, or UV Map/Mapping "
      "for chosen coordinates. W drives 1D/4D noise and can be animated. Normalize affects fBM; Detail is clamped to "
      "0..15 by Cycles."}},
    {"layer_weight",
     {"Layer Weight", "Input",
      "Facing and Fresnel factors from Normal. An unconnected Normal uses the surface shading normal."}},
    {"light_path", {"Light Path", "Input", "Cycles ray type and path information."}},
    {"math",
     {"Math", "Converter",
      "Scalar operations. Operation tokens are stored by name, independently of engine enum values."}},
    {"mix_closure",
     {"Mix Shader", "Shader",
      "Blend two shaders in the same surface or volume domain. Factor zero selects Shader 1; one selects Shader 2. Use "
      "Mix to blend colors before a single shader."}},
    {"normal",
     {"Normal", "Input",
      "Normal direction and dot product. An unconnected Normal follows the upstream Cycles default."}},
    {"object_info",
     {"Object Info", "Input", "Cycles object information. Attribute availability depends on the geometry adapter."}},
    {"principled_bsdf",
     {"Principled BSDF", "Shader",
      "Layered Cycles surface with spectral transmission dispersion. Dispersion Scale and Abbe Number control solid "
      "transmission; Nested Dielectrics evaluates relative medium IOR at the same wavelength. Unconnected normal and "
      "tangent inputs use engine defaults."}},
    {"principled_volume",
     {"Principled Volume", "Volume",
      "Density is multiplied by Density Attribute when that field name is nonempty. Temperature is the named grid "
      "multiplied by the Kelvin scale. Emission Strength needs an explicit field mask to follow sparse pyro."}},
    {"rgb_to_bw", {"RGB to BW", "Converter", "Convert RGB to a scalar using Cycles luminance semantics."}},
    {"separate_xyz", {"Separate XYZ", "Converter", "Split a vector into scalar components."}},
    {"scatter_volume",
     {"Volume Scatter", "Volume",
      "Scatter light per world-space length. Connect the density field explicitly. Phase and anisotropy use the pinned "
      "Cycles semantics."}},
    {"value", {"Value", "Input", "A scalar constant; supports Houdini animation and channel references."}},
    {"vector_math", {"Vector Math", "Converter", "Vector operations. Scale is a scalar; vector values are triples."}},
    {"vector_displacement",
     {"Vector Displacement", "Vector",
      "Vector offsets in object, world or tangent space. The default is tangent space, as in Blender. Tangent "
      "space accepts the default uv or a named two/three-component mesh UV field, with X along tangent, Y along "
      "normal and Z along signed bitangent. Deformation samples compatible tangent bases. True/Both changes mesh "
      "positions."}},
    {"volume_info",
     {"Volume Info", "Input",
      "Read the standard density, color, flame and temperature grids. Only fields used by the output graph are "
      "required."}},
    {"ambient_occlusion",
     {"Ambient Occlusion", "Input",
      "Trace local occlusion rays. Distance is in world units; Only Local limits intersections to this object. This "
      "node does not enable Fast GI."}},
    {"bevel",
     {"Bevel", "Vector",
      "Shader bevel normal from additional rays. Radius is in scene units. Connect to a shader Normal; mesh topology "
      "is unchanged."}},
    {"blackbody",
     {"Blackbody", "Converter",
      "Convert a temperature in kelvin to Cycles blackbody RGB in the rendering color space."}},
    {"brick_texture",
     {"Brick Texture", "Texture",
      "Procedural bricks and mortar. Unconnected Vector uses world position; use Mapping for chosen coordinates."}},
    {"brightness_contrast",
     {"Brightness/Contrast", "Color", "Adjust scene-linear RGB with the Cycles brightness and contrast formula."}},
    {"camera_info",
     {"Camera Data", "Input", "View vector, axial depth and geometric camera distance at the shading point."}},
    {"checker_texture",
     {"Checker Texture", "Texture",
      "Procedural checker cells. Unconnected Vector uses world position in Houdini units."}},
    {"clamp",
     {"Clamp", "Converter", "Clamp a scalar using Min Max or Range behavior. Enum values are stored as named tokens."}},
    {"combine_color", {"Combine Color", "Converter", "Combine components in RGB, HSV or HSL space."}},
    {"gabor_texture",
     {"Gabor Texture", "Texture",
      "Oriented 2D/3D Gabor noise. Orientation angles are radians. Unconnected Vector uses world position."}},
    {"gamma", {"Gamma", "Color", "Apply the Cycles per-channel gamma operation in scene-linear RGB."}},
    {"glossy_bsdf",
     {"Glossy BSDF", "Shader",
      "Glossy reflection with microfacet distribution, roughness, anisotropy and tangent orientation."}},
    {"gradient_texture",
     {"Gradient Texture", "Texture", "Procedural gradient with named modes. Unconnected Vector uses world position."}},
    {"hair_bsdf",
     {"Hair BSDF", "Shader",
      "Legacy reflection/transmission hair model. Use on native curve geometry; Tangent retains its engine default."}},
    {"hair_info",
     {"Curves Info", "Input",
      "Native curve identity, intercept, control-polyline length, thickness and tangent normal. Intercept measures "
      "distance along the SOP control keys."}},
    {"hsv",
     {"Hue/Saturation/Value", "Color",
      "Adjust hue, saturation and value with a blend factor using Cycles RGB semantics."}},
    {"invert", {"Invert Color", "Color", "Blend between the input RGB and its one-minus complement."}},
    {"light_falloff",
     {"Light Falloff", "Input",
      "Cycles quadratic, linear and constant falloff factors. Evaluate in the shader's ray context."}},
    {"magic_texture",
     {"Magic Texture", "Texture", "Procedural recursive color texture. Unconnected Vector uses world position."}},
    {"map_range",
     {"Map Range", "Converter",
      "Remap a scalar with linear, stepped, smoothstep or smootherstep interpolation and optional clamping."}},
    {"metallic_bsdf",
     {"Metallic BSDF", "Shader",
      "Conductor reflection with artistic edge tint or physical complex IOR. IOR and extinction coefficient are RGB "
      "triples."}},
    {"mix_color",
     {"Mix", "Color",
      "Mix values, vectors or colors A and B using Factor. Data Type selects the inputs and result. Vector Factor Mode "
      "can be Uniform or Non-Uniform per XYZ component. Color has additional blend modes and independent Clamp Result. "
      "Clamp Factor limits Factor to 0..1; disable it to extrapolate. Use Mix Shader to combine shaders."}},
    {"point_info", {"Point Info", "Input", "Native sphere position, radius and stable point random value."}},
    {"principled_hair_bsdf",
     {"Principled Hair BSDF", "Shader",
      "Chiang/Huang hair scattering, color or melanin parametrization and stable per-curve randomization."}},
    {"radial_tiling",
     {"Radial Tiling", "Vector",
      "Cyclic coordinate segments from the pinned Cycles engine. Unconnected Vector uses world position."}},
    {"ray_portal_bsdf",
     {"Ray Portal BSDF", "Shader",
      "Transport a ray to a position and direction. Coordinates are in Cycles world space; color modulates transported "
      "radiance."}},
    {"raycast",
     {"Raycast", "Input",
      "Trace a shading ray and report hit data. Position, direction and length use world space; Only Local restricts "
      "the object."}},
    {"refraction_bsdf",
     {"Refraction BSDF", "Shader",
      "Microfacet refraction without the reflection component. IOR is the relative index of refraction."}},
    {"scene_time",
     {"Scene Time", "Input",
      "Houdini nominal render frame and time in seconds. Frame 1 is time zero at the host frame rate."}},
    {"separate_color", {"Separate Color", "Converter", "Split RGB, HSV or HSL components."}},
    {"set_normal", {"Set Normal", "Vector", "Normalize a direction for use as a shading normal."}},
    {"sheen_bsdf",
     {"Sheen BSDF", "Shader", "Soft fabric sheen with the pinned Cycles distribution and roughness models."}},
    {"subsurface_scattering",
     {"Subsurface Scattering", "Shader",
      "Random-walk/diffusion scattering. Radius and Scale use Houdini distance units."}},
    {"tangent",
     {"Tangent", "Input",
      "Polygon-mesh tangent from the default uv or a named two/three-component UV field, or a radial tangent from "
      "normalized SOP bounding-box coordinates. Deformation samples UV-derived bases across the shutter. Missing "
      "fields recover locally; point/hair UV tangent requests use the geometry recovery policy."}},
    {"texture_coordinate",
     {"Texture Coordinate", "Input",
      "Generated uses normalized nominal SOP bounds, UV uses the default uv map, Object uses local geometry space. "
      "Other outputs use engine camera/ray coordinates. Instancer/reference-object coordinates are diagnosed as "
      "unsupported."}},
    {"toon_bsdf", {"Toon BSDF", "Shader", "Cycles diffuse or glossy toon response with Size and Smooth controls."}},
    {"translucent_bsdf", {"Translucent BSDF", "Shader", "Diffuse transmission through a surface."}},
    {"vector_map_range",
     {"Map Range", "Vector", "Remap each vector component with named interpolation and optional clamping."}},
    {"vector_rotate",
     {"Vector Rotate", "Vector",
      "Rotate coordinates about Center by an axis/angle or Euler rotation. Angles are radians."}},
    {"vector_transform",
     {"Vector Transform", "Vector", "Convert points, vectors or normals between object, world and camera spaces."}},
    {"vertex_color",
     {"Color Attribute", "Input",
      "Read named numeric Houdini color fields in the render working space. Empty Layer Name selects Cd. Deformation "
      "motion samples compatible colors across the shutter. Missing fields recover locally; data values are not color "
      "transformed."}},
    {"volume_coefficients",
     {"Volume Coefficients", "Volume",
      "Direct scattering, absorption and emission coefficients with the pinned engine's phase functions. Coefficients "
      "measure inverse distance."}},
    {"voronoi_texture",
     {"Voronoi Texture", "Texture",
      "1D..4D Voronoi distances, colors and positions with named metrics/features. Unconnected Vector uses world "
      "position."}},
    {"wave_texture",
     {"Wave Texture", "Texture",
      "Bands or rings with distortion, detail and animatable phase. Unconnected Vector uses world position."}},
    {"wavelength", {"Wavelength", "Converter", "Convert a visible-light wavelength in nanometers to Cycles RGB."}},
    {"white_noise_texture",
     {"White Noise Texture", "Texture",
      "Deterministic hashed 1D..4D noise. Unconnected Vector uses world position; W supplies the additional "
      "coordinate."}},
    {"wireframe",
     {"Wireframe", "Input",
      "Shading mask near tessellated triangle edges, in world or pixel units. It does not modify SOP topology."}},
    {"float_curve",
     {"Float Curve", "Converter",
      "Map Value through the inline native float ramp, then blend with the original using Factor. Ramp position 0..1 "
      "spans Range; ramp values are the output (HDR and negative values are kept). The renderer samples the shape at "
      "257 positions. Outside Range, Extension extrapolates the end slopes or holds the end values. Replaces Float "
      "Ramp: Range 0..1, Extension Horizontal and Factor 1 map Value like Float Ramp mapped its Factor. Invalid "
      "controls recover locally."}},
    {"rgb_curves",
     {"RGB Curves", "Color",
      "Apply the Combined ramp to each component, then the Red, Green and Blue ramps. All ramps are inline native "
      "float ramps over Range. Cycles evaluates the combined shape from 257 samples. Factor blends with the original "
      "color. No "
      "compositor black/white balancing or film-like tone control."}},
    {"vector_curves",
     {"Vector Curves", "Vector",
      "Map X, Y and Z independently through inline native float ramps over Range (default -1..1). Each key's basis "
      "shapes the 257-entry table. Factor blends with the original vector; values outside Range follow Extension."}}};
struct ParmStorage {
  PRM_Name name;
  std::array<PRM_Default, 3> defaults;
  std::string string_default, help;
  std::vector<PRM_Name> menu_names;
  std::unique_ptr<PRM_ChoiceList> menu;
  std::unique_ptr<PRM_Conditional> conditional;
  PRM_Range range{PRM_RANGE_UI, 0, PRM_RANGE_UI, 1};
  PRM_SpareData spare;
};
struct Definition {
  ch::ShaderDefinition shader;
  std::string operator_name, label, category, help;
  std::vector<ch::ShaderSocketDefinition> inputs, outputs;
  std::set<std::string> input_group_labels;
  std::deque<ParmStorage> storage;
  std::vector<PRM_Template> templates;
  PRM_Name index_count_name{"item_count", "Items"}, index_item_name{"item#", "Value #"};
  PRM_Default index_count_default{2};
  std::array<PRM_Template, 2> index_item_templates;
  struct Folder {
    PRM_Name name;
    PRM_Default size;
  };
  std::deque<Folder> folders;
  struct CurveRamp {
    PRM_Name name;
    PRM_Default size{2};
    PRM_SpareData spare;
    std::string token, defaults, help;
    std::unique_ptr<PRM_Template, decltype(&PRMfreeRampTemplate)> children{nullptr, PRMfreeRampTemplate};
  };
  std::deque<CurveRamp> curve_ramps;
  std::vector<PRM_Default> curve_tabs;
  PRM_Name curve_tab_name{"curve_channels", "Channels"};
  std::unique_ptr<PRM_Template, decltype(&PRMfreeRampTemplate)> ramp_children{nullptr, PRMfreeRampTemplate};
  std::unique_ptr<PRM_Template, decltype(&PRMfreeRampTemplate)> alpha_children{nullptr, PRMfreeRampTemplate};
  PRM_SpareData ramp_spare, alpha_spare;
  std::unique_ptr<PRM_Conditional> ramp_basis_disable, alpha_basis_disable;
  PRM_Name ramp_name{"ramp", "Ramp"}, alpha_name{"alpha_ramp", "Alpha"};
  PRM_Default ramp_size{2}, alpha_size{2};
  std::string alpha_help;
  bool builder{}, output{}, volume_builder{}, world_builder{}, light_builder{};
};
std::vector<std::unique_ptr<Definition>> &definitions() {
  static std::vector<std::unique_ptr<Definition>> data;
  return data;
}
bool editable(const ch::ShaderSocketDefinition &socket) {
  // Normals/tangents with implicit engine links are ports, not constant UI
  // values. Leaving them unconnected must preserve automatic defaults.
  return !socket.internal_svm && !socket.internal_osl && socket.authoring_default && socket.default_links.empty();
}
bool curve_kind(const std::string &kind) {
  return kind == "float_curve" || kind == "rgb_curves" || kind == "vector_curves";
}
// Scenes saved before the inline Color/Alpha ramps stored RGBA stops in the
// Color ramp's value child (ramp#c with four components, alpha channel
// ramp#ca). Houdini would silently drop that fourth component on load, so the
// saved text is rewritten before the host parses it: alpha moves to the
// independent Alpha ramp at the same positions, with the stop's basis (or
// Linear where HSV/HSL ignored it). Only that legacy layout is touched.
std::vector<std::string> split_lines(const std::string &text) {
  std::vector<std::string> lines;
  size_t start = 0;
  while (start <= text.size()) {
    const auto end = text.find('\n', start);
    if (end == std::string::npos) {
      lines.push_back(text.substr(start));
      break;
    }
    lines.push_back(text.substr(start, end - start));
    start = end + 1;
  }
  return lines;
}
std::string join_lines(const std::vector<std::string> &lines) {
  std::string text;
  for (size_t i = 0; i < lines.size(); ++i) {
    text += lines[i];
    if (i + 1 < lines.size())
      text += '\n';
  }
  return text;
}
struct SavedParm {
  std::string name, values;
  size_t open{}, close{};
  bool ok{};
};
SavedParm saved_parm(const std::string &line) {
  SavedParm parm;
  const auto tab = line.find('\t');
  if (tab == std::string::npos)
    return parm;
  parm.name = line.substr(0, tab);
  const auto flags = line.find('[', tab);
  const auto flags_end = flags == std::string::npos ? flags : line.find(']', flags);
  if (flags_end == std::string::npos)
    return parm;
  parm.open = line.find('(', flags_end);
  parm.close = line.rfind(')');
  if (parm.open == std::string::npos || parm.close == std::string::npos || parm.close < parm.open)
    return parm;
  parm.values = line.substr(parm.open + 1, parm.close - parm.open - 1);
  parm.ok = true;
  return parm;
}
std::vector<std::string> saved_tokens(const std::string &values) {
  // Plain values, quoted strings and "[ channel default ]" animated values.
  std::vector<std::string> tokens;
  size_t i = 0;
  while (i < values.size()) {
    while (i < values.size() && std::isspace(static_cast<unsigned char>(values[i])))
      ++i;
    if (i >= values.size())
      break;
    const auto start = i;
    if (values[i] == '[') {
      const auto end = values.find(']', i);
      i = end == std::string::npos ? values.size() : end + 1;
    } else if (values[i] == '"') {
      ++i;
      while (i < values.size() && values[i] != '"') {
        if (values[i] == '\\')
          ++i;
        ++i;
      }
      ++i;
    } else
      while (i < values.size() && !std::isspace(static_cast<unsigned char>(values[i])))
        ++i;
    tokens.push_back(values.substr(start, std::min(i, values.size()) - start));
  }
  return tokens;
}
bool legacy_stop(const std::string &name, const char *suffix, int &index) {
  const auto length = std::strlen(suffix);
  if (!name.starts_with("ramp") || !name.ends_with(suffix) || name.size() <= 4 + length)
    return false;
  const auto digits = name.substr(4, name.size() - 4 - length);
  if (digits.size() > 4 ||
      !std::all_of(digits.begin(), digits.end(), [](char c) { return std::isdigit(static_cast<unsigned char>(c)); }))
    return false;
  index = std::stoi(digits);
  return true;
}
std::string renamed_channel(const std::string &token, const std::string &name) {
  // "[ old\tdefault ]" -> "[ name\tdefault ]".
  const auto start = token.find_first_not_of(" \t", 1);
  const auto end = start == std::string::npos ? start : token.find_first_of(" \t]", start);
  if (end == std::string::npos)
    return token;
  return token.substr(0, start) + name + token.substr(end);
}
std::string channel_default(const std::string &token) {
  if (token.empty() || token[0] != '[')
    return token;
  const auto parts = saved_tokens(token.substr(1, token.rfind(']') - 1));
  return parts.size() >= 2 ? parts[1] : "0";
}
bool legacy_rgba_parms(const std::string &text) {
  for (const auto &line : split_lines(text)) {
    const auto parm = saved_parm(line);
    int index = 0;
    if (parm.ok && parm.name == "alpha_ramp")
      return false;
    if (parm.ok && legacy_stop(parm.name, "c", index) && saved_tokens(parm.values).size() == 4)
      return true;
  }
  return false;
}
std::string channel_name(const std::string &line) {
  const auto trimmed = line.find_first_not_of(" \t");
  if (trimmed == std::string::npos || line.compare(trimmed, 8, "channel ") != 0)
    return {};
  const auto start = trimmed + 8;
  const auto end = line.find_first_of(" \t{", start);
  return line.substr(start, end == std::string::npos ? std::string::npos : end - start);
}
std::string migrate_legacy_rgba_channels(const std::string &text, std::set<std::string> &duplicated) {
  const auto lines = split_lines(text);
  std::vector<std::string> result;
  for (size_t i = 0; i < lines.size(); ++i) {
    const auto name = channel_name(lines[i]);
    if (name.empty()) {
      result.push_back(lines[i]);
      continue;
    }
    // A channel block ends where its braces balance, ignoring quoted text.
    size_t end = i;
    int depth = 0;
    bool quoted = false;
    for (; end < lines.size(); ++end) {
      for (size_t c = 0; c < lines[end].size(); ++c) {
        const char ch = lines[end][c];
        if (ch == '\\') {
          ++c;
          continue;
        }
        if (ch == '"')
          quoted = !quoted;
        else if (!quoted && ch == '{')
          ++depth;
        else if (!quoted && ch == '}')
          --depth;
      }
      if (depth <= 0)
        break;
    }
    if (end >= lines.size())
      end = lines.size() - 1;
    const std::vector<std::string> block(lines.begin() + i, lines.begin() + end + 1);
    const auto rename = [&](std::vector<std::string> copy, const std::string &target) {
      const auto at = copy[0].find(name);
      copy[0].replace(at, name.size(), target);
      return copy;
    };
    int index = 0;
    if (legacy_stop(name, "ca", index)) {
      const auto target = "alpha_ramp" + std::to_string(index) + "value";
      for (const auto &line : rename(block, target))
        result.push_back(line);
      duplicated.insert(target);
    } else {
      result.insert(result.end(), block.begin(), block.end());
      if (legacy_stop(name, "pos", index)) {
        const auto target = "alpha_ramp" + std::to_string(index) + "pos";
        for (const auto &line : rename(block, target))
          result.push_back(line);
        duplicated.insert(target);
      } else if (name == "ramp") {
        // A shared Color Ramp reference ch(".../ramp") also shares its
        // Alpha ramp. Other count expressions keep their current value.
        auto copy = rename(block, "alpha_ramp");
        bool shared = false;
        for (size_t n = 1; n < copy.size(); ++n)
          for (const auto *quote : {"/ramp\\\"", "/ramp\"", "/ramp'"})
            for (auto at = copy[n].find(quote); at != std::string::npos; at = copy[n].find(quote, at + 11)) {
              copy[n].replace(at, 5, "/alpha_ramp");
              shared = true;
            }
        if (shared) {
          result.insert(result.end(), copy.begin(), copy.end());
          duplicated.insert("alpha_ramp");
        }
      }
    }
    i = end;
  }
  return join_lines(result);
}
std::string migrate_legacy_rgba_parms(const std::string &text, const std::set<std::string> &duplicated) {
  const auto lines = split_lines(text);
  std::vector<std::string> result;
  std::map<int, std::string> alpha, position, basis;
  std::string count = "2", space;
  for (const auto &line : lines) {
    const auto parm = saved_parm(line);
    if (!parm.ok)
      continue;
    const auto tokens = saved_tokens(parm.values);
    if (tokens.empty())
      continue;
    int index = 0;
    if (parm.name == "ramp")
      count = tokens[0];
    else if (parm.name == "ramp_color_space")
      space = tokens[0];
    else if (legacy_stop(parm.name, "c", index) && tokens.size() == 4)
      alpha[index] = tokens[3];
    else if (legacy_stop(parm.name, "pos", index))
      position[index] = tokens[0];
    else if (legacy_stop(parm.name, "interp", index))
      basis[index] = tokens[0];
  }
  space = channel_default(space);
  if (space.size() >= 2 && space.front() == '"')
    space = space.substr(1, space.size() - 2);
  const bool hue = space == "HSV" || space == "HSL";
  const auto value = [&](const std::string &token, const std::string &channel) {
    if (token.empty() || token[0] != '[')
      return token;
    return duplicated.contains(channel) ? renamed_channel(token, channel) : channel_default(token);
  };
  const auto line = [](const std::string &name, const std::string &token) {
    return name + "\t[ 0\tlocks=0 ]\t(\t" + token + "\t)";
  };
  std::vector<std::string> added{line("alpha_ramp", value(count, "alpha_ramp"))};
  for (const auto &[index, token] : alpha) {
    const auto prefix = "alpha_ramp" + std::to_string(index);
    added.push_back(line(prefix + "pos", value(position.contains(index) ? position[index] : "0", prefix + "pos")));
    added.push_back(line(prefix + "value", value(token, prefix + "value")));
    // HSV/HSL blended legacy alpha linearly, independently of stop bases.
    added.push_back(line(prefix + "interp",
                         hue ? "\"linear\"" : channel_default(basis.contains(index) ? basis[index] : "\"linear\"")));
  }
  bool inserted = false;
  for (const auto &text_line : lines) {
    const auto parm = saved_parm(text_line);
    int index = 0;
    if (parm.ok && parm.name == "edit_ramp")
      continue; // Retired editor button.
    if (parm.ok && legacy_stop(parm.name, "c", index)) {
      const auto tokens = saved_tokens(parm.values);
      if (tokens.size() == 4) {
        result.push_back(text_line.substr(0, parm.open + 1) + "\t" + tokens[0] + "\t" + tokens[1] + "\t" + tokens[2] +
                         "\t" + text_line.substr(parm.close));
        continue;
      }
    }
    if (!inserted && text_line == "}") {
      result.insert(result.end(), added.begin(), added.end());
      inserted = true;
    }
    result.push_back(text_line);
  }
  return join_lines(result);
}
void curve_parameters(Definition &definition) {
  // Value on top, then Range/Extension and the inline channel ramps, then
  // Factor. Each channel is a native float ramp edited in the pane.
  const bool vector = definition.shader.engine_type == "vector_curves";
  auto root = std::move(definition.templates);
  definition.templates.clear();
  const auto take = [&](const std::string &id) {
    for (const auto &item : root)
      if (item.getToken() && id == item.getToken())
        definition.templates.push_back(item);
  };
  take("value");
  auto &range = definition.storage.emplace_back();
  range.name = PRM_Name("curve_domain", "Range");
  range.defaults[0] = PRM_Default(vector ? -1.f : 0.f);
  range.defaults[1] = PRM_Default(1.f);
  range.help = "Input values mapped across the ramps: ramp position 0 is the Range start and 1 its end. Ramp values "
               "are output values; HDR and negative values are kept.";
  definition.templates.emplace_back(PRM_XYZ, 2, &range.name, range.defaults.data(), nullptr, nullptr, nullptr, nullptr,
                                    1, range.help.c_str());
  auto &extension = definition.storage.emplace_back();
  extension.name = PRM_Name("curve_extension", "Extension");
  for (const auto &[token, label] : std::vector<std::pair<const char *, const char *>>{{"extrapolated", "Extrapolated"},
                                                                                       {"horizontal", "Horizontal"}})
    extension.menu_names.emplace_back(PRM_Name::COPY, UT_StringHolder(token), UT_StringHolder(label));
  extension.menu_names.emplace_back();
  extension.menu = std::make_unique<PRM_ChoiceList>(PRM_CHOICELIST_SINGLE, extension.menu_names.data());
  extension.defaults[0] = PRM_Default(0);
  extension.help = "Values outside Range. Extrapolated continues each ramp's slope at its first and last key; "
                   "Horizontal holds the end values, like a native ramp lookup.";
  definition.templates.emplace_back(PRM_ORD, 1, &extension.name, extension.defaults.data(), extension.menu.get(),
                                    nullptr, nullptr, nullptr, 1, extension.help.c_str());
  std::vector<std::pair<std::string, std::string>> channels;
  if (definition.shader.engine_type == "rgb_curves")
    channels = {{"c", "Combined"}, {"r", "R"}, {"g", "G"}, {"b", "B"}};
  else if (vector)
    channels = {{"x", "X"}, {"y", "Y"}, {"z", "Z"}};
  else
    channels = {{"value", "Curve"}};
  if (channels.size() > 1) {
    // One compact tab strip: a single ramp gadget is visible at a time.
    definition.curve_tabs.clear();
    for (const auto &channel : channels) {
      auto &label = definition.storage.emplace_back();
      label.string_default = channel.second;
      definition.curve_tabs.emplace_back(1, label.string_default.c_str());
    }
    definition.templates.emplace_back(PRM_SWITCHER, int(channels.size()), &definition.curve_tab_name,
                                      definition.curve_tabs.data());
  }
  for (const auto &[channel, label] : channels) {
    auto &ramp = definition.curve_ramps.emplace_back();
    ramp.token = "curve_" + channel;
    ramp.name = PRM_Name(PRM_Name::COPY, UT_StringHolder(ramp.token), UT_StringHolder(label));
    ramp.defaults = std::string("1pos ( 0 ) 1value ( ") + (vector ? "-1" : "0") +
                    " ) 1interp ( linear ) 2pos ( 1 ) 2value ( 1 ) 2interp ( linear )";
    ramp.help = channel == "c" ? "Applied to each component before the R, G and B ramps. Positions span Range; values "
                                 "are output values. The renderer samples the shape at 257 positions."
                               : "Positions span Range; values are output values (HDR and negative values are kept). "
                                 "The renderer samples the shape at 257 positions.";
    ramp.spare.setRampBasisDefault("linear");
    ramp.spare.addTokenValue(PRM_SPARE_RAMP_FLOAT_DEFAULT_TOKEN, ramp.defaults.c_str());
    ramp.children.reset(
        PRMcreateRampTemplate(ramp.token.c_str(), PRM_MULTITYPE_RAMP_FLT, &ramp.spare, nullptr, nullptr));
    definition.templates.emplace_back(PRM_MULTITYPE_RAMP_FLT, ramp.children.get(), 1., &ramp.name, &ramp.size, nullptr,
                                      &ramp.spare, ramp.help.c_str());
  }
  take("fac");
  for (const auto &item : root)
    if (!item.getToken() || (std::string(item.getToken()) != "value" && std::string(item.getToken()) != "fac"))
      definition.templates.push_back(item);
}
// Material > Settings (Blender names). Engine Shader sockets of the pinned
// version: emission_sampling_method, use_transparent_shadow,
// use_bump_map_correction, volume_sampling_method,
// volume_interpolation_method, volume_step_rate.
const char *material_setting_label(const std::string &socket, const std::string &token) {
  if (socket != "cycles_emission_sampling" && socket != "cycles_volume_sampling" &&
      socket != "cycles_volume_interpolation")
    return token.c_str();
  static const std::map<std::string, const char *> labels{{"none", "None"},
                                                          {"auto", "Auto"},
                                                          {"front", "Front"},
                                                          {"back", "Back"},
                                                          {"front_back", "Front and Back"},
                                                          {"distance", "Distance"},
                                                          {"equiangular", "Equiangular"},
                                                          {"multiple_importance", "Multiple Importance"},
                                                          {"linear", "Linear"},
                                                          {"cubic", "Cubic"}};
  const auto found = labels.find(token);
  return found == labels.end() ? token.c_str() : found->second;
}
const char *material_setting_help(const std::string &socket) {
  if (socket == "cycles_emission_sampling")
    return "Light sampling of emissive surfaces. Auto picks Front, Back or both from the shader; None stops the "
           "surface from being a sampled light (it is still seen and still lights the scene through bounces). Use "
           "None for many small or faint emitters, for example heavily instanced shapes, to save light-tree memory "
           "and time.";
  if (socket == "cycles_transparent_shadows")
    return "Use transparent shadows when the material contains a Transparent BSDF. Off renders faster but the "
           "material then casts a fully opaque shadow.";
  if (socket == "cycles_bump_map_correction")
    return "Apply corrections to solve shadow terminator artifacts caused by bump mapping.";
  if (socket == "cycles_volume_sampling")
    return "Sampling method for this material's volume. Multiple Importance suits most cases; Equiangular can "
           "reduce noise near lights inside a thin volume; Distance suits dense volumes lit from outside.";
  if (socket == "cycles_volume_interpolation")
    return "Interpolation of volume grids (VDB and Houdini volumes). Cubic is smoother on low-resolution grids and "
           "slower.";
  if (socket == "cycles_volume_step_rate")
    return "Scales the distance between volume shader samples. Lower is more accurate and slower. Used only when "
           "Biased Volume Ray Marching is on in the Cycles ROP.";
  return nullptr;
}
// Material > Settings follow Displacement Method and Pass Index, after one
// separator. No folder: Houdini rebuilds an operator's own switcher as a
// duplicate spare folder whenever a VOP node's parameter interface is edited
// (promote_parameter, Edit Parameter Interface; SideFX VOPs such as Inline do
// the same), and a scene saved after that edit warns on load about the
// unrecognized duplicate. Builders are where artists edit the interface.
void material_settings_separator(Definition &definition) {
  const auto first = std::find_if(definition.templates.begin(), definition.templates.end(), [](const auto &item) {
    const std::string token = item.getToken() ? item.getToken() : "";
    return token == "cycles_emission_sampling" || token == "cycles_volume_sampling";
  });
  if (first == definition.templates.begin() || first == definition.templates.end())
    return; // Cycles Volume Material: its volume settings are its only parameters.
  static PRM_Name separator("cycles_settings_separator", "");
  definition.templates.insert(first, PRM_Template(PRM_SEPARATOR, 0, &separator));
}
// A newly created node has Blender 5.3.0's default wherever the input is the same
// thing in Blender (read from Blender itself: blender -b --factory-startup, a node
// added to a new material). These replace the reflected standalone Cycles defaults,
// which Blender's own sync never uses. Saved scenes and Blender imports keep the
// values they store. docs/user/rop-defaults-and-layout.md, "Defaults follow Blender".
void blender_defaults(Definition &definition) {
  using C = std::array<float, 3>;
  static const std::map<std::string, std::vector<std::pair<std::string, ch::ShaderValue>>> table = {
      {"blackbody", {{"temperature", 6500.f}}},
      {"brick_texture", {{"color1", C{.8f, .8f, .8f}}, {"color2", C{.2f, .2f, .2f}}, {"mortar_smooth", .1f}}},
      {"brightness_contrast", {{"color", C{1.f, 1.f, 1.f}}}},
      {"bump", {{"distance", .001f}}},
      {"checker_texture", {{"color1", C{.8f, .8f, .8f}}, {"color2", C{.2f, .2f, .2f}}, {"scale", 5.f}}},
      {"color", {{"value", C{.5f, .5f, .5f}}}},
      {"displacement", {{"scale", .01f}}},
      {"emission", {{"color", C{1.f, 1.f, 1.f}}, {"strength", 1.f}}},
      {"enable_output_boolean", {{"enable", false}}},
      {"enable_output_color", {{"enable", false}, {"value", C{.8f, .8f, .8f}}}},
      {"enable_output_float", {{"enable", false}}},
      {"enable_output_integer", {{"enable", false}}},
      {"enable_output_shader", {{"enable", false}}},
      {"enable_output_vector", {{"enable", false}}},
      {"gamma", {{"color", C{1.f, 1.f, 1.f}}}},
      {"glass_bsdf", {{"color", C{1.f, 1.f, 1.f}}, {"distribution", std::string("multi_ggx")}}},
      {"glossy_bsdf", {{"distribution", std::string("multi_ggx")}}},
      {"hair_bsdf", {{"roughness_u", .1f}, {"roughness_v", 1.f}}},
      {"hsv", {{"color", C{.8f, .8f, .8f}}}},
      {"map_range", {{"clamp", true}}},
      {"math", {{"value3", .5f}}},
      {"noise_texture", {{"scale", 5.f}}},
      {"normal", {{"direction", C{0.f, 0.f, 1.f}}, {"normal", C{0.f, 0.f, 1.f}}}},
      {"normal_map", {{"base", std::string("displaced")}}},
      {"principled_hair_bsdf", {{"model", std::string("Chiang")}}},
      {"refraction_bsdf", {{"color", C{1.f, 1.f, 1.f}}, {"IOR", 1.45f}, {"distribution", std::string("beckmann")}}},
      {"rgb_to_bw", {{"color", C{.5f, .5f, .5f}}}},
      {"separate_color", {{"color", C{.8f, .8f, .8f}}}},
      {"sheen_bsdf", {{"roughness", .5f}}},
      {"switch_color", {{"false", C{.8f, .8f, .8f}}, {"true", C{.8f, .8f, .8f}}}},
      {"tangent", {{"axis", std::string("z")}}},
      {"vector_displacement", {{"scale", .01f}, {"space", std::string("tangent")}, {"vector", C{.8f, .8f, .8f}}}},
      {"vector_map_range", {{"use_clamp", true}}},
      {"vector_transform", {{"vector", C{.5f, .5f, .5f}}}},
      {"voronoi_texture", {{"smoothness", 1.f}}},
      {"warning", {{"message", std::string()}, {"severity", std::string("error")}}},
      {"wave_texture", {{"scale", 5.f}, {"detail_scale", 1.f}}}};
  const auto found = table.find(definition.shader.engine_type);
  if (found == table.end())
    return;
  for (const auto &[id, value] : found->second) {
    auto socket = std::find_if(definition.shader.inputs.begin(), definition.shader.inputs.end(),
                               [&](const auto &s) { return s.id == id; });
    if (socket == definition.shader.inputs.end() || !socket->authoring_default ||
        socket->authoring_default->index() != value.index() ||
        (!socket->enum_values.empty() &&
         std::none_of(socket->enum_values.begin(), socket->enum_values.end(),
                      [&](const auto &entry) { return entry.token == std::get<std::string>(value); })))
      throw std::logic_error("Blender default does not match the Cycles input: " + definition.shader.engine_type + "/" +
                             id);
    socket->authoring_default = value;
  }
}
void parameters(Definition &definition) {
  const bool sky = definition.shader.engine_type == "sky_texture";
  const bool mix = ch::native_shader_is_mix(definition.shader.engine_type);
  const bool shader_combine =
      definition.shader.engine_type == "mix_closure" || definition.shader.engine_type == "add_closure";
  if (curve_kind(definition.shader.engine_type)) {
    for (auto &socket : definition.shader.inputs) {
      if (socket.id == "fac") {
        socket.authoring_default = 1.f;
        socket.label = "Factor";
      }
      if (socket.id == "value")
        socket.authoring_default =
            definition.shader.engine_type == "float_curve"
                ? ch::ShaderValue(1.f)
                : ch::ShaderValue(std::array<float, 3>{definition.shader.engine_type == "rgb_curves" ? 1.f : 0.f,
                                                       definition.shader.engine_type == "rgb_curves" ? 1.f : 0.f,
                                                       definition.shader.engine_type == "rgb_curves" ? 1.f : 0.f});
    }
    // Value is the primary port on top, Factor at the bottom: ports and
    // parameters follow this order. Links resolve by socket name.
    std::stable_partition(definition.shader.inputs.begin(), definition.shader.inputs.end(),
                          [](const auto &socket) { return socket.id != "fac"; });
  }
  if (definition.shader.engine_type == "mix_color") {
    // Blender exposes one Mix node. Its typed Cycles nodes are private
    // lowering targets, not separate choices in the native node menu.
    // Blender's authored defaults differ from standalone Cycles' backend
    // defaults. This is the Color-menu entry of the single Mix node.
    for (auto &socket : definition.shader.inputs) {
      if (socket.id == "a" || socket.id == "b")
        socket.authoring_default = std::array<float, 3>{.5f, .5f, .5f};
      else if (socket.id == "fac")
        socket.authoring_default = 1.f;
    }
    ch::ShaderSocketDefinition data_type;
    data_type.id = "data_type";
    data_type.label = "Data Type";
    data_type.type = "enum";
    data_type.authoring_default = std::string("color");
    data_type.enum_values = {{"float", 0}, {"vector", 1}, {"color", 2}};
    auto factor_mode = data_type;
    factor_mode.id = "factor_mode";
    factor_mode.label = "Factor Mode";
    factor_mode.authoring_default = std::string("uniform");
    factor_mode.enum_values = {{"uniform", 0}, {"non_uniform", 1}};
    const auto input = [&](const char *name) {
      return *std::find_if(definition.shader.inputs.begin(), definition.shader.inputs.end(),
                           [&](const auto &s) { return s.id == name; });
    };
    const auto a = input("a"), b = input("b"), fac = input("fac");
    definition.shader.inputs.insert(definition.shader.inputs.begin(), {data_type, factor_mode});
    for (const auto &[source, id, type] :
         std::vector<std::tuple<ch::ShaderSocketDefinition, std::string, std::string>>{{a, "a_float", "float"},
                                                                                       {b, "b_float", "float"},
                                                                                       {a, "a_vector", "vector"},
                                                                                       {b, "b_vector", "vector"},
                                                                                       {fac, "fac_vector", "vector"}}) {
      auto socket = source;
      socket.id = id;
      socket.type = type;
      socket.linkable = false; // Three stable ports adapt their types; pane defaults are per mode.
      socket.authoring_default =
          type == "float"
              ? ch::ShaderValue(0.f)
              : ch::ShaderValue(std::array<float, 3>{id == "fac_vector" ? .5f : 0.f, id == "fac_vector" ? .5f : 0.f,
                                                     id == "fac_vector" ? .5f : 0.f});
      definition.shader.inputs.push_back(std::move(socket));
    }
  }
  blender_defaults(definition);
  if (mix || shader_combine) {
    // Artist-facing labels do not replace the engine's versioned socket IDs.
    for (auto &socket : definition.shader.inputs)
      socket.label = ch::native_shader_short_label(definition.shader.engine_type, socket.id, socket.label);
    for (auto &socket : definition.shader.outputs)
      if (socket.id == "closure")
        socket.label = "Shader";
  }
  if (sky) {
    // Authored order follows Blender's node, with the native orientation
    // control last. Keep socket IDs and the reflected engine catalogue.
    const std::vector<std::string> order = {
        "sky_type",      "sun_disc",  "sun_size",      "sun_intensity",       "sun_elevation",
        "sun_rotation",  "altitude",  "air_density",   "aerosol_density",     "ozone_density",
        "sun_direction", "turbidity", "ground_albedo", "tex_mapping.rotation"};
    const auto rank = [&](const auto &socket) {
      return std::find(order.begin(), order.end(), socket.id) - order.begin();
    };
    std::stable_sort(definition.shader.inputs.begin(), definition.shader.inputs.end(),
                     [&](const auto &a, const auto &b) { return rank(a) < rank(b); });
  }
  if (definition.shader.engine_type == "fresnel" || definition.shader.engine_type == "layer_weight")
    // Normal follows IOR/Blend as in Blender's node, which also keeps the
    // input index of wires saved before Normal was a port.
    std::stable_partition(definition.shader.inputs.begin(), definition.shader.inputs.end(),
                          [](const auto &socket) { return socket.id != "normal"; });
  for (const auto &socket : definition.shader.inputs) {
    if (definition.shader.engine_type.starts_with("index_switch_") &&
        (socket.id == "item_count" || socket.id.starts_with("item")))
      continue;
    if (curve_kind(definition.shader.engine_type) && !socket.linkable)
      continue;
    // OSL-internal inputs (Fresnel and Layer Weight Normal) are ports: only
    // their unconnected default is evaluated inside the OSL shader (N).
    if (socket.internal_svm)
      continue;
    if (definition.shader.engine_type == "bump" &&
        (socket.id == "sample_center" || socket.id == "sample_x" || socket.id == "sample_y"))
      continue;
    if (socket.linkable)
      definition.inputs.push_back(socket);
    // Texture transforms belong to the Mapping node; Sky Rotation is native.
    // New authoring uses Mapping without discarding existing HIP values.
    if (socket.id.starts_with("tex_mapping.") && !(sky && socket.id == "tex_mapping.rotation"))
      continue;
    if (!editable(socket)) {
      // Native VOP grouping follows parameter ownership, including for
      // implicit engine inputs. Invisible labels supply group metadata
      // without introducing an editable normal/tangent constant.
      if (socket.linkable && (shader_combine || !ch::native_shader_panels(definition.shader.engine_type).empty())) {
        auto &parm = definition.storage.emplace_back();
        parm.name = PRM_Name(PRM_Name::COPY, UT_StringHolder(socket.id), UT_StringHolder(socket.label));
        definition.templates.emplace_back(PRM_LABEL, 1, &parm.name);
        definition.templates.back().setInvisible(true);
        definition.input_group_labels.insert(socket.id);
      }
      continue;
    }
    auto &parm = definition.storage.emplace_back();
    parm.name = PRM_Name(
        PRM_Name::COPY, UT_StringHolder(socket.id),
        UT_StringHolder(ch::native_shader_short_label(definition.shader.engine_type, socket.id, socket.label)));
    parm.help = definition.help;
    const auto &kind = definition.shader.engine_type;
    if (socket.id == "attribute" &&
        (kind == "uvmap" || kind == "tangent" || kind == "normal_map" || kind == "vector_displacement")) {
      parm.name = PRM_Name("attribute", "UV Map");
      parm.help = "Empty or uv uses Houdini's default UV map. A named numeric field needs two or three components; "
                  "tangent generation uses XY. Deformation samples compatible fields and tangent bases over the "
                  "shutter. Missing or incompatible maps recover locally.";
    } else if (kind == "color_ramp" && socket.id == "ramp_interpolation")
      parm.help = "Native (Per Stop) uses each stop's basis on both ramps; the inline ramps show the render. Linear, "
                  "Constant, Ease, B-Spline and Cardinal are Blender's modes for both Color and Alpha. The inline "
                  "ramps cannot draw Ease, B-Spline or Cardinal and preview Houdini's per-stop interpolation instead.";
    else if (kind == "color_ramp" && socket.id == "ramp_color_space")
      parm.help = "Color blending space. HSV/HSL follow Hue Interpolation; the inline Color ramp previews RGB "
                  "blending, not the hue path. LAB uses each stop's basis. Alpha is unaffected.";
    else if (kind == "color_ramp" && socket.id == "ramp_hue_interpolation")
      parm.help =
          "HSV/HSL hue direction between Color stops. The inline Color ramp previews RGB blending, not this hue path.";
    else if (kind == "normal_map" && socket.id == "base")
      parm.help = "Surface shading can use Original or Displaced geometry. Displacement evaluation always uses the "
                  "Original basis, including when this same node also shades the displaced surface.";
    int size = 1;
    PRM_Type type = PRM_FLT;
    std::visit(
        [&](const auto &value) {
          using T = std::decay_t<decltype(value)>;
          if constexpr (std::is_same_v<T, bool>) {
            type = PRM_TOGGLE;
            parm.defaults[0] = PRM_Default(value ? 1 : 0);
          } else if constexpr (std::is_same_v<T, int>) {
            type = PRM_INT;
            parm.defaults[0] = PRM_Default(value);
          } else if constexpr (std::is_same_v<T, float>)
            parm.defaults[0] = PRM_Default(value);
          else if constexpr (std::is_same_v<T, std::array<float, 3>>) {
            size = 3;
            type = socket.type == "color" ? PRM_RGB : PRM_XYZ;
            for (int i = 0; i < 3; ++i)
              parm.defaults[i] = PRM_Default(value[i]);
          } else if constexpr (std::is_same_v<T, std::string>) {
            type = PRM_STRING;
            parm.string_default = value;
            parm.defaults[0] = PRM_Default(0, parm.string_default.c_str(), CH_STRING_LITERAL);
          } else if constexpr (std::is_same_v<T, std::vector<int>>) {
            type = PRM_STRING;
            for (int entry : value) {
              if (!parm.string_default.empty())
                parm.string_default += ' ';
              parm.string_default += std::to_string(entry);
            }
            parm.defaults[0] = PRM_Default(0, parm.string_default.c_str(), CH_STRING_LITERAL);
          }
        },
        *socket.authoring_default);
    if (sky) {
      if (socket.id == "sun_size" || socket.id == "sun_elevation" || socket.id == "sun_rotation")
        parm.defaults[0] = PRM_Default(std::get<float>(*socket.authoring_default) * 180.0f / std::numbers::pi_v<float>);
      if (socket.id == "tex_mapping.rotation") {
        // Engine dot-named sockets do not retain Houdini channels.
        // This new authored node uses a native animation-safe name.
        parm.name = PRM_Name("sky_rotation", "Sky Rotation");
        parm.defaults = {PRM_Default(90), PRM_Default(0), PRM_Default(0)};
        parm.help = "Degrees. Default (90, 0, 0) maps Houdini Y up into the sky model's Z up. Set (0, 0, 0) for Cycles "
                    "Z up. This engine mapping preserves direct solar-disc sampling; a separate linked Mapping node "
                    "uses map sampling instead.";
      } else if (socket.id == "sun_direction")
        parm.help = "Nonzero direction in sky-model coordinates (Z up), normalized by the adapter. Sky Rotation places "
                    "that model in Houdini. Used only by Preetham and Hosek/Wilkie.";
      else if (socket.id == "sun_size")
        parm.help = "Solar angular diameter in degrees, 0..180. Cycles applies a small internal minimum for numerical "
                    "precision. Used only when Sun Disc is on.";
      else if (socket.id == "sun_elevation" || socket.id == "sun_rotation")
        parm.help = "Degrees in sky-model coordinates. Elevation is above the horizon; rotation is around the model's "
                    "up axis. Values may wrap for animated day/night cycles.";
      else if (socket.id == "altitude")
        parm.help = "Metres above sea level, 0..100000. Cycles internally clamps Single Scattering below 60 km and "
                    "Multiple Scattering below 100 km. Camera position does not set this parameter.";
    }
    if ((definition.shader.engine_type == "image_texture" || definition.shader.engine_type == "environment_texture" ||
         definition.shader.engine_type == "ies_light") &&
        socket.id == "filename")
      type = PRM_FILE;
    if (definition.shader.engine_type == "image_texture" && socket.id == "tiles")
      parm.help = "Optional space-separated UDIM IDs, 1001..9999. Empty discovers tiles matching the filename token.";
    if ((definition.shader.engine_type == "image_texture" || definition.shader.engine_type == "environment_texture") &&
        socket.id == "colorspace") {
      parm.string_default = "auto";
      parm.defaults[0] = PRM_Default(0, parm.string_default.c_str(), CH_STRING_LITERAL);
      parm.help = "Auto uses the current Houdini filename mode and OCIO file rules, then image metadata and format "
                  "defaults. Data bypasses color conversion. The menu follows active OCIO spaces; saved aliases and "
                  "inactive names may also be entered explicitly.";
      parm.menu = std::make_unique<PRM_ChoiceList>(PRM_CHOICELIST_REPLACE, texture_colorspace_menu);
    } else if (definition.shader.engine_type == "attribute" && socket.id == "attribute") {
      // Houdini's first supported attribute is Cd. This is an authored
      // host default; the reflected engine default remains an empty name.
      parm.string_default = "Cd";
      parm.defaults[0] = PRM_Default(0, parm.string_default.c_str(), CH_STRING_LITERAL);
      for (const char *name :
           {"Cd", "point_random", "curve_random", "density", "temperature", "color", "flame", "heat", "velocity"})
        parm.menu_names.emplace_back(PRM_Name::COPY, UT_StringHolder(name), UT_StringHolder(name));
      parm.menu_names.emplace_back();
      // Suggestions, not an enum: custom volume field names are valid.
      // Unknown surface attributes render with a local diagnostic fallback.
      parm.menu = std::make_unique<PRM_ChoiceList>(PRM_CHOICELIST_REPLACE, parm.menu_names.data());
      parm.help = "Read a named numeric Houdini field, Cd, point_random or curve_random on surfaces, or a named volume "
                  "grid. Numeric tuples have one, two or three components. Type a name or choose a suggestion. "
                  "Missing/incompatible fields recover locally; Deformation samples compatible surface fields across "
                  "the shutter. Volume grids follow the separate volume recovery policy.";
    } else if (!socket.enum_values.empty()) {
      if (sky && socket.id == "tex_mapping.type") {
        // Blender authors Sky with point mapping; standalone Cycles'
        // reflected texture default instead inverts the rotation.
        parm.string_default = "point";
        parm.defaults[0] = PRM_Default(0, parm.string_default.c_str(), CH_STRING_LITERAL);
      }
      parm.menu_names.reserve(socket.enum_values.size() + 1);
      if (sky && socket.id == "sky_type") {
        for (const auto &[token, label] :
             std::vector<std::pair<const char *, const char *>>{{"multiple_scattering", "Multiple Scattering"},
                                                                {"single_scattering", "Single Scattering (Legacy)"},
                                                                {"preetham", "Preetham (Legacy)"},
                                                                {"hosek_wilkie", "Hosek / Wilkie (Legacy)"}})
          parm.menu_names.emplace_back(PRM_Name::COPY, UT_StringHolder(token), UT_StringHolder(label));
      } else if (kind == "color_ramp" && socket.id == "ramp_hue_interpolation") {
        for (const auto &[token, label] : std::vector<std::pair<const char *, const char *>>{
                 {"NEAR", "Near"}, {"FAR", "Far"}, {"CW", "Clockwise"}, {"CCW", "Counter-Clockwise"}})
          parm.menu_names.emplace_back(PRM_Name::COPY, UT_StringHolder(token), UT_StringHolder(label));
      } else if (kind == "color_ramp" && socket.id == "ramp_interpolation") {
        for (const auto &[token, label] :
             std::vector<std::pair<const char *, const char *>>{{"LINEAR", "Linear"},
                                                                {"CONSTANT", "Constant"},
                                                                {"EASE", "Ease"},
                                                                {"B_SPLINE", "B-Spline"},
                                                                {"CARDINAL", "Cardinal"},
                                                                {"NATIVE", "Native (Per Stop)"}})
          parm.menu_names.emplace_back(PRM_Name::COPY, UT_StringHolder(token), UT_StringHolder(label));
      } else
        for (const auto &entry : socket.enum_values)
          parm.menu_names.emplace_back(PRM_Name::COPY, UT_StringHolder(entry.token),
                                       UT_StringHolder(material_setting_label(socket.id, entry.token)));
      parm.menu_names.emplace_back();
      parm.menu = std::make_unique<PRM_ChoiceList>(PRM_CHOICELIST_SINGLE, parm.menu_names.data());
    }
    if (definition.shader.engine_type == "principled_bsdf" && socket.id == "transmission_dispersion_scale")
      parm.help = "Spectral dispersion for solid transmission, from 0 (disabled) to 1 (full material dispersion). The "
                  "material IOR is specified at the Fraunhofer d line; Abbe Number sets the wavelength variation. Thin "
                  "Wall does not use this solid-medium model.";
    if (definition.shader.engine_type == "principled_bsdf" && socket.id == "transmission_dispersion_abbe_number")
      parm.help = "Abbe number: (IOR at the d line minus 1) divided by the difference between the F-line and C-line "
                  "IORs. Smaller positive values disperse more; zero disables dispersion. Common glass values are "
                  "around 20 to 80. With Nested Dielectrics, both media are evaluated at the same path wavelength.";
    PRM_Range *range = nullptr;
    if (const char *help = material_setting_help(socket.id))
      parm.help = help;
    if (socket.id == "cycles_volume_step_rate") {
      parm.range = PRM_Range(PRM_RANGE_RESTRICTED, 0.001, PRM_RANGE_UI, 10);
      range = &parm.range;
    }
    if (socket.id == "cycles_pass_index") {
      parm.range = PRM_Range(PRM_RANGE_RESTRICTED, 0, PRM_RANGE_RESTRICTED, 32767);
      range = &parm.range;
      parm.help = "Unfiltered Material Index, 0..32767. Enable Material Index under Passes on the Cycles ROP. This "
                  "identifies the material; it does not modify shading or displacement.";
    }
    if (socket.type == "float" && socket.id != "cycles_volume_step_rate") {
      range = &parm.range;
      if (sky) {
        if (socket.id == "sun_size")
          parm.range = PRM_Range(PRM_RANGE_RESTRICTED, 0, PRM_RANGE_RESTRICTED, 180);
        else if (socket.id == "sun_elevation")
          parm.range = PRM_Range(PRM_RANGE_UI, -90, PRM_RANGE_UI, 90);
        else if (socket.id == "sun_rotation")
          parm.range = PRM_Range(PRM_RANGE_UI, 0, PRM_RANGE_UI, 360);
        else if (socket.id == "sun_intensity")
          parm.range = PRM_Range(PRM_RANGE_RESTRICTED, 0, PRM_RANGE_UI, 10);
        else if (socket.id == "altitude")
          parm.range = PRM_Range(PRM_RANGE_RESTRICTED, 0, PRM_RANGE_RESTRICTED, 100000);
        else if (socket.id == "turbidity")
          parm.range = PRM_Range(PRM_RANGE_RESTRICTED, 1, PRM_RANGE_RESTRICTED, 10);
        else if (socket.id.ends_with("_density"))
          parm.range = PRM_Range(PRM_RANGE_RESTRICTED, 0, PRM_RANGE_RESTRICTED, 10);
      } else if (definition.shader.engine_type == "noise_texture" && socket.id == "detail")
        parm.range = PRM_Range(PRM_RANGE_UI, 0, PRM_RANGE_UI, 15);
      else if (definition.shader.engine_type == "noise_texture" && (socket.id == "scale" || socket.id == "distortion"))
        parm.range = PRM_Range(PRM_RANGE_UI, 0, PRM_RANGE_UI, 10);
      else if (definition.shader.engine_type == "noise_texture" && socket.id == "lacunarity")
        parm.range = PRM_Range(PRM_RANGE_UI, 0, PRM_RANGE_UI, 4);
      else if (socket.id == "strength" || socket.id == "emission_strength")
        parm.range = PRM_Range(PRM_RANGE_UI, 0, PRM_RANGE_UI, 10);
      else if (socket.id == "transmission_dispersion_abbe_number")
        parm.range = PRM_Range(PRM_RANGE_UI, 0, PRM_RANGE_UI, 100);
      else if (socket.id == "anisotropy")
        parm.range = PRM_Range(PRM_RANGE_UI, -.99, PRM_RANGE_UI, .99);
      else if (socket.id == "temperature")
        parm.range = PRM_Range(PRM_RANGE_UI, 0, PRM_RANGE_UI, 5000);
      else if (socket.id == "ior" || socket.id == "IOR" || socket.id.ends_with("_ior"))
        parm.range = PRM_Range(PRM_RANGE_UI, 1, PRM_RANGE_UI, 3);
      else if (socket.id.starts_with("value"))
        range = nullptr;
    }
    definition.templates.emplace_back(type, size, &parm.name, parm.defaults.data(), parm.menu.get(), range, nullptr,
                                      nullptr, 1, parm.help.c_str());
    if (sky) {
      const auto modern = "{ sky_type == preetham } { sky_type == hosek_wilkie }";
      const auto legacy = "{ sky_type != preetham sky_type != hosek_wilkie }";
      const char *hide = nullptr;
      if (socket.id == "sun_disc" || socket.id == "sun_elevation" || socket.id == "sun_rotation" ||
          socket.id == "altitude" || socket.id.ends_with("_density"))
        hide = modern;
      else if (socket.id == "sun_size" || socket.id == "sun_intensity")
        hide = "{ sky_type == preetham } { sky_type == hosek_wilkie } { sun_disc == 0 }";
      else if (socket.id == "sun_direction" || socket.id == "turbidity")
        hide = legacy;
      else if (socket.id == "ground_albedo")
        hide = "{ sky_type != hosek_wilkie }";
      if (hide) {
        parm.conditional = std::make_unique<PRM_Conditional>(hide, PRM_CONDTYPE_HIDE);
        definition.templates.back().setConditionalBasePtr(parm.conditional.get());
      }
    }
    const auto hidden = ch::shader_ui_hide_when(ch::native_shader_hidden(kind, socket.id));
    if (!hidden.empty()) {
      parm.conditional = std::make_unique<PRM_Conditional>(hidden.c_str(), PRM_CONDTYPE_HIDE);
      definition.templates.back().setConditionalBasePtr(parm.conditional.get());
    }
    if (kind == "color_ramp" && socket.id == "ramp_hue_interpolation") {
      parm.conditional =
          std::make_unique<PRM_Conditional>("{ ramp_color_space != HSV ramp_color_space != HSL }", PRM_CONDTYPE_HIDE);
      definition.templates.back().setConditionalBasePtr(parm.conditional.get());
    }
    if (kind == "color_ramp" && socket.id == "ramp_interpolation") {
      parm.conditional = std::make_unique<PRM_Conditional>("{ ramp_color_space != RGB }", PRM_CONDTYPE_HIDE);
      definition.templates.back().setConditionalBasePtr(parm.conditional.get());
    }
    if (kind == "ies_light" && socket.id == "ies")
      definition.templates.back().setInvisible(
          true); // Owned inline text is available through the graph API; native authoring uses File Name.
  }
  if (mix) {
    // Flat VOP connectors follow input indices, independently of parameter
    // order. Reorder the host ports too; their stable names let Houdini
    // restore existing named HIP connections without changing engine IDs.
    std::stable_partition(definition.inputs.begin(), definition.inputs.end(),
                          [](const auto &socket) { return socket.id != "fac" && socket.id != "fac_vector"; });
    // Hidden shader labels supply ordering metadata without introducing
    // editable shader constants in the parameter pane.
    const auto rank = [&](const PRM_Template &item) {
      const auto socket = std::find_if(definition.shader.inputs.begin(), definition.shader.inputs.end(),
                                       [&](const auto &input) { return input.id == item.getToken(); });
      return socket == definition.shader.inputs.end() || !socket->linkable ? 0
             : socket->id == "fac" || socket->id == "fac_vector"           ? 2
                                                                           : 1;
    };
    std::stable_sort(definition.templates.begin(), definition.templates.end(),
                     [&](const auto &a, const auto &b) { return rank(a) < rank(b); });
  }
  if (definition.builder)
    material_settings_separator(definition);
  const auto panels = ch::native_shader_panels(definition.shader.engine_type);
  if (!panels.empty()) {
    // Reorder only parameter templates. Versioned input indices and socket
    // tokens are independent and stay stable for existing saved wiring.
    const auto flat = definition.templates;
    definition.templates.clear();
    std::map<std::string, const PRM_Template *> by_name;
    std::set<std::string> grouped;
    for (const auto &item : flat)
      by_name.emplace(item.getToken(), &item);
    for (const auto &panel : panels)
      for (const auto &name : panel.sockets)
        grouped.insert(name);
    std::vector<PRM_Template> root;
    for (const auto &item : flat)
      if (!grouped.contains(item.getToken()))
        root.push_back(item);
    if (!root.empty()) {
      auto &folder = definition.folders.emplace_back();
      folder.name = PRM_Name("cycles_surface", "Surface");
      folder.size = PRM_Default(float(root.size()), folder.name.getLabel());
      definition.templates.emplace_back(PRM_SWITCHER, 1, &folder.name, &folder.size, nullptr, nullptr, nullptr,
                                        &PRM_SpareData::groupTypeSimple);
      definition.templates.insert(definition.templates.end(), root.begin(), root.end());
    }
    for (const auto &panel : panels) {
      std::vector<PRM_Template> children;
      for (const auto &name : panel.sockets)
        if (const auto found = by_name.find(name); found != by_name.end())
          children.push_back(*found->second);
      if (children.empty())
        continue;
      auto &folder = definition.folders.emplace_back();
      folder.name = PRM_Name(PRM_Name::COPY, UT_StringHolder("cycles_" + panel.name), UT_StringHolder(panel.label));
      // PRM_Default retains its label pointer: use the stable name label.
      folder.size = PRM_Default(float(children.size()), folder.name.getLabel());
      definition.templates.emplace_back(PRM_SWITCHER, 1, &folder.name, &folder.size, nullptr, nullptr, nullptr,
                                        panel.closed ? &PRM_SpareData::groupTypeCollapsible
                                                     : &PRM_SpareData::groupTypeCollapsibleExpanded);
      definition.templates.insert(definition.templates.end(), children.begin(), children.end());
    }
  }
  if (definition.shader.engine_type.starts_with("index_switch_")) {
    const auto type = definition.shader.outputs.front().type;
    const auto parm_type = type == "int"       ? PRM_INT
                           : type == "boolean" ? PRM_TOGGLE
                           : type == "color"   ? PRM_RGB
                           : type == "vector"  ? PRM_XYZ
                                               : PRM_FLT;
    // Blender's Index Switch items in Color mode default to 0.8 grey (blender_defaults).
    static PRM_Default blender_grey[] = {PRM_Default(.8f), PRM_Default(.8f), PRM_Default(.8f)};
    definition.index_item_templates[0] =
        PRM_Template(type == "closure" ? PRM_LABEL : parm_type, type == "vector" || type == "color" ? 3 : 1,
                     &definition.index_item_name, type == "color" ? blender_grey : PRMzeroDefaults);
    definition.templates.emplace_back(PRM_MULTITYPE_LIST, definition.index_item_templates.data(), 2,
                                      &definition.index_count_name, &definition.index_count_default, nullptr,
                                      &PRM_SpareData::multiStartOffsetZero);
  }
  if (curve_kind(definition.shader.engine_type))
    curve_parameters(definition);
  definition.templates.emplace_back();
  definition.outputs = definition.shader.outputs;
}
void type_info(VOP_TypeInfo &info, const std::string &type) {
  if (type == "closure")
    info.setType(VOP_TYPE_BSDF);
  else if (type == "color" || type == "vector" || type == "normal" || type == "point")
    info.setType(VOP_TYPE_VECTOR);
  else if (type == "float")
    info.setType(VOP_TYPE_FLOAT);
  else if (type == "int" || type == "boolean")
    info.setType(VOP_TYPE_INTEGER);
  else if (type == "string" || type == "enum")
    info.setType(VOP_TYPE_STRING);
  else
    info.setType(VOP_TYPE_UNDEF);
}
class CyclesVop;
class CyclesOperator final : public VOP_Operator {
public:
  explicit CyclesOperator(Definition &data);
  UT_Color getDefaultColor() const override {
    // Builders are what artists create in /mat: Cycles Material and Cycles
    // Volume Material keep Houdini's default node color; the World Material
    // is blue. Shader nodes inside builders keep Blender's category colors.
    if (definition.world_builder)
      return UT_Color(UT_RGB, 74 / 255., 144 / 255., 226 / 255.);
    if (definition.builder)
      return VOP_Operator::getDefaultColor();
    return ch::native_shader_node_color(definition.category, definition.shader.engine_type);
  }
  Definition &definition;
};
class Filter final : public OP_OperatorFilter {
public:
  explicit Filter(bool world = false, bool light = false) : world_(world), light_(light) {}
  bool allowOperatorAsChild(OP_Operator *op) override {
    if (ch::is_cycles_subnet_operator(*op))
      return true;
    const auto *cycles = dynamic_cast<CyclesOperator *>(op);
    if (!cycles || cycles->definition.builder)
      return false;
    const auto &definition = cycles->definition;
    if (light_) {
      if (ch::is_cycles_ramp_operator(*op))
        return true;
      if (definition.output)
        return definition.operator_name == "cycles::light_output::1";
      static const auto allowed = ch::light_graph_definitions();
      return std::find(allowed.begin(), allowed.end(), definition.shader.id) != allowed.end();
    }
    if (!world_)
      return definition.shader.id != "cycles/background_shader/1" &&
             definition.operator_name != "cycles::world_output::1" &&
             definition.operator_name != "cycles::light_output::1";
    if (ch::is_cycles_ramp_operator(*op))
      return true;
    if (definition.output)
      return definition.operator_name == "cycles::world_output::1";
    static const auto allowed = ch::world_graph_definitions();
    return std::find(allowed.begin(), allowed.end(), definition.shader.id) != allowed.end();
  }

private:
  bool world_{};
  bool light_{};
};
class CyclesVop final : public VOP_Node {
public:
  CyclesVop(OP_Network *net, const char *name, OP_Operator *op)
      : VOP_Node(net, name, op), data_(static_cast<CyclesOperator *>(op)->definition),
        filter_(data_.world_builder, data_.light_builder) {
    setLanguage("Cycles");
    if (data_.builder) {
      setMaterialFlag(true);
      ch::set_native_shader_identity(*this);
      thumbnail_owner_ = ch::install_material_thumbnail(*this);
    }
  }
  static OP_Node *construct(OP_Network *net, const char *name, OP_Operator *op) { return new CyclesVop(net, name, op); }
  bool runCreateScript() override {
    if (!VOP_Node::runCreateScript())
      return false;
    // Saved user data survives HIP reloads. Fresh node creation receives a
    // new identity; network extraction remains read-only.
    ch::set_native_shader_identity(*this);
    if (data_.shader.engine_type == "principled_volume")
      for (auto *parent = getParent(); parent; parent = parent->getParent()) {
        const auto *parent_operator = parent->getOperator();
        if (!parent_operator || parent_operator->getName() != "cycles::world_material::1")
          continue;
        // Blender's object-volume defaults read grids. An unbounded world
        // has no geometry fields: use the authored constant inputs there.
        for (const auto *name : {"density_attribute", "color_attribute", "temperature_attribute"})
          setString("", CH_STRING_LITERAL, name, 0, CHgetEvalTime());
        break;
      }
    if (!data_.builder && !data_.output)
      for (const auto &output : data_.outputs) {
        const auto input = ch::native_shader_bypass_input(*this, output.id);
        auto *modes = getOutputInfoManager();
        modes->setBypassedOutputMode(output.id.c_str(),
                                     input.empty() ? VOP_OUTPUT_MODE_CONSTANT : VOP_OUTPUT_MODE_PASSTHROUGH);
        if (!input.empty())
          modes->setBypassCorrespondingInputName(output.id.c_str(), input.c_str());
        modes->setUseDefaultValue(output.id.c_str(), true);
      }
    if (data_.builder) {
      auto *surface = createNodeOfExactType(data_.world_builder    ? "cycles::background_shader::1"
                                            : data_.volume_builder ? "cycles::principled_volume::1"
                                            : data_.light_builder  ? "cycles::emission::1"
                                                                   : "cycles::principled_bsdf::1",
                                            data_.world_builder    ? "background_shader"
                                            : data_.volume_builder ? "volume_shader"
                                            : data_.light_builder  ? "emission"
                                                                   : "surface_shader");
      auto *output = createNodeOfExactType(data_.world_builder    ? "cycles::world_output::1"
                                           : data_.volume_builder ? "cycles::volume_output::1"
                                           : data_.light_builder  ? "cycles::light_output::1"
                                                                  : "cycles::surface_output::1",
                                           data_.world_builder    ? "world_output"
                                           : data_.volume_builder ? "volume_output"
                                           : data_.light_builder  ? "light_output"
                                                                  : "surface_output");
      if (surface && output) {
        if (data_.light_builder) {
          surface->setFloat("strength", 0, CHgetEvalTime(), 1.f);
          for (int component = 0; component < 3; ++component)
            surface->setFloat("color", component, CHgetEvalTime(), 1.f);
        }
        output->setInput(0, surface, 0);
        // Shader left, output right, tops aligned (a VOP's position is
        // its title), one node-spacing apart: the artist's reference
        // layout. VOP widths include the expanded input labels.
        const fpreal width = surface->getW() > 0 ? surface->getW() : 3.;
        output->setXY(surface->getX() + width + 1., surface->getY());
      }
    }
    return true;
  }
  const char *getChildType() const override { return data_.builder ? VOP_TABLE_NAME : nullptr; }
  void getActiveOperatorsDefinedByOTL(UT_Set<OP_Operator *> &active) const override {
    VOP_Node::getActiveOperatorsDefinedByOTL(active);
    ch::collect_scene_definitions(*this, active, data_.builder);
  }
  OP_OpTypeId getChildTypeID() const override { return data_.builder ? VOP_OPTYPE_ID : INVALID_OPTYPE_ID; }
  OP_OperatorFilter *getOperatorFilter() override { return data_.builder ? &filter_ : VOP_Node::getOperatorFilter(); }
  bool canDefineShader() const override { return data_.builder; }
  bool isShader() const override { return data_.builder; }
  bool isShader(VOP_Type type) const override { return data_.builder && type == VOP_SURFACE_SHADER; }
  VOP_Type getShaderType() const override { return data_.builder ? VOP_SURFACE_SHADER : VOP_GENERIC_SHADER; }
  unsigned getNumVisibleInputs() const override {
    return index_switch() ? unsigned(index_count() + 1) : unsigned(data_.inputs.size());
  }
  unsigned getNumVisibleOutputs() const override { return unsigned(data_.outputs.size()); }
  const char *inputLabel(OP_InputIdx index) const override {
    if (index_switch() && index > 0) {
      while (dynamic_labels_.size() < std::size_t(index))
        dynamic_labels_.push_back(std::to_string(dynamic_labels_.size()));
      return dynamic_labels_[index - 1].c_str();
    }
    return index >= 0 && index < data_.inputs.size() ? data_.inputs[index].label.c_str() : "";
  }
  const char *outputLabel(OP_OutputIdx index) const override {
    return index >= 0 && index < data_.outputs.size() ? data_.outputs[index].label.c_str() : "";
  }
  void opChanged(OP_EventType reason, void *data = nullptr) override {
    VOP_Node::opChanged(reason, data);
    ch::ensure_material_thumbnail(*this, thumbnail_owner_);
    if ((reason == OP_PARM_CHANGED || reason == OP_PARM_ANIMATED || reason == OP_PARM_CHANNELS ||
         reason == OP_INPUT_REWIRED) &&
        !refreshing_) {
      refresh_inputs();
      if (data_.shader.engine_type == "mix_color")
        dirtyCachedOutputData();
      setVisibleConnectorsDirty();
    }
  }
  bool load(UT_IStream &is, const char *ext = "", const char *path = nullptr) override {
    // Legacy RGBA Color Ramp stops migrate while their saved text is read.
    // Channels precede parameter values in a saved node; hold them until
    // the values show whether this node uses the legacy RGBA layout.
    std::string section = ext ? ext : "";
    if (section.starts_with("."))
      section.erase(0, 1);
    if (data_.shader.engine_type != "color_ramp" || is.isBinary() || !is.isSubStream() ||
        (section != "chn" && section != "parm"))
      return VOP_Node::load(is, ext, path);
    UT_WorkBuffer buffer;
    is.getAll(buffer);
    std::string text(buffer.buffer(), buffer.length());
    if (section == "chn") {
      pending_channels_ = std::move(text);
      pending_ext_ = ext ? ext : "";
      has_pending_channels_ = true;
      return true;
    }
    const bool legacy = legacy_rgba_parms(text);
    std::set<std::string> duplicated;
    bool loaded = true;
    if (has_pending_channels_) {
      auto channels = legacy ? migrate_legacy_rgba_channels(pending_channels_, duplicated) : pending_channels_;
      UT_IStream stream(channels.data(), channels.size(), UT_ISTREAM_ASCII);
      loaded = VOP_Node::load(stream, pending_ext_.c_str(), path);
      has_pending_channels_ = false;
      pending_channels_.clear();
    }
    if (legacy)
      text = migrate_legacy_rgba_parms(text, duplicated);
    UT_IStream stream(text.data(), text.size(), UT_ISTREAM_ASCII);
    return VOP_Node::load(stream, ext, path) && loaded;
  }

protected:
  void finishedLoadingNetwork(bool child_call = false) override {
    if (has_pending_channels_) {
      // A channel section without parameter values: load it unchanged.
      UT_IStream stream(pending_channels_.data(), pending_channels_.size(), UT_ISTREAM_ASCII);
      VOP_Node::load(stream, pending_ext_.c_str());
      has_pending_channels_ = false;
      pending_channels_.clear();
    }
    VOP_Node::finishedLoadingNetwork(child_call);
    refresh_inputs();
    setVisibleConnectorsDirty();
  }
  bool getIsInputVisibleDefaultSubclass(int index) override {
    if (index_switch())
      return index >= 0 && index <= index_count();
    return index >= 0 && index < data_.inputs.size() &&
           ch::native_shader_input_active(*this, data_.inputs[index].id, CHgetEvalTime());
  }
  bool updateParmsFlags() override {
    const bool changed = VOP_Node::updateParmsFlags();
    return refresh_inputs() || changed;
  }
  OP_ERROR cookMe(OP_Context &) override { return error(); }
  void getInputNameSubclass(UT_String &name, int index) const override {
    if (index_switch() && index > 0) {
      name.harden(("item" + std::to_string(index - 1)).c_str());
      return;
    }
    name = index >= 0 && index < data_.inputs.size() ? data_.inputs[index].id.c_str() : "";
  }
  int getInputFromNameSubclass(const UT_String &name) const override {
    if (index_switch() && name.toStdString().starts_with("item")) {
      const auto value = name.toStdString().substr(4);
      if (!value.empty() && value.size() <= 4 && value.find_first_not_of("0123456789") == std::string::npos) {
        const auto index = std::stoi(value);
        return index < index_count() ? index + 1 : -1;
      }
      return -1;
    }
    for (int i = 0; i < data_.inputs.size(); ++i)
      if (name == data_.inputs[i].id.c_str())
        return i;
    return -1;
  }
  void getInputTypeInfoSubclass(VOP_TypeInfo &info, int index) override {
    if (index_switch() && index > 0) {
      type_info(info, data_.shader.outputs.front().type);
      return;
    }
    if (data_.shader.engine_type == "mix_color" && index >= 0 && index < data_.inputs.size()) {
      UT_String kind, mode;
      evalString(kind, "data_type", 0, CHgetEvalTime());
      evalString(mode, "factor_mode", 0, CHgetEvalTime());
      const auto &id = data_.inputs[index].id;
      type_info(info, id == "fac"       ? (kind == "vector" && mode == "non_uniform" ? "vector" : "float")
                      : kind == "float" ? "float"
                                        : "vector");
    } else
      type_info(info, index >= 0 && index < data_.inputs.size() ? data_.inputs[index].type : "");
  }
  void getAllowedInputTypeInfosSubclass(unsigned index, VOP_VopTypeInfoArray &infos) override {
    VOP_TypeInfo info;
    getInputTypeInfoSubclass(info, int(index));
    infos.append(info);
    // Cycles converts numeric inputs to unit-strength emission. Keep the
    // socket's shader identity while allowing the host to wire a preview.
    if ((index < data_.inputs.size() && data_.inputs[index].type == "closure") ||
        (index_switch() && index > 0 && data_.shader.outputs.front().type == "closure"))
      for (const auto type : {VOP_TYPE_FLOAT, VOP_TYPE_VECTOR, VOP_TYPE_INTEGER}) {
        VOP_TypeInfo numeric;
        numeric.setType(type);
        infos.append(numeric);
      }
  }
  void getOutputNameSubclass(UT_String &name, int index) const override {
    name = index >= 0 && index < data_.outputs.size() ? data_.outputs[index].id.c_str() : "";
  }
  void getOutputTypeInfoSubclass(VOP_TypeInfo &info, int index) override {
    if (data_.shader.engine_type == "mix_color" && index == 0) {
      UT_String kind;
      evalString(kind, "data_type", 0, CHgetEvalTime());
      type_info(info, kind == "float" ? "float" : "vector");
    } else
      type_info(info, index >= 0 && index < data_.outputs.size() ? data_.outputs[index].type : "");
  }

private:
  bool index_switch() const { return data_.shader.engine_type.starts_with("index_switch_"); }
  int index_count() const {
    return std::clamp(int(evalInt("item_count", 0, CHgetEvalTime())), 0, OP_MULTI_INPUT_MAX - 1);
  }
  mutable std::deque<std::string> dynamic_labels_;
  ch::MaterialThumbnailLease thumbnail_owner_;
  bool refresh_inputs() {
    if (refreshing_)
      return false;
    refreshing_ = true;
    bool changed = false;
    if (data_.shader.engine_type == "mix_color") {
      UT_String kind;
      evalString(kind, "data_type", 0, CHgetEvalTime());
      const auto category = [](const std::string &type) {
        return type == "float" ? "Converter" : type == "vector" ? "Vector" : "Color";
      };
      if (kind != mix_type_.c_str()) {
        if (getColor() == ch::native_shader_node_color(category(mix_type_)))
          setColor(ch::native_shader_node_color(category(kind.toStdString())));
        mix_type_ = kind.toStdString();
      }
      for (const auto &[port, parms] :
           std::map<std::string, std::vector<std::string>>{{"a", {"a", "a_float", "a_vector"}},
                                                           {"b", {"b", "b_float", "b_vector"}},
                                                           {"fac", {"fac", "fac_vector"}}})
        for (const auto &name : parms)
          changed |= enableParm(name.c_str(), !isConnected(getInputFromName(UT_String(port.c_str())), false));
    }
    for (int i = 0; i < data_.inputs.size(); ++i) {
      const auto &socket = data_.inputs[i];
      if (data_.input_group_labels.contains(socket.id))
        changed |= setVisibleState(socket.id.c_str(), false);
      if (editable(socket)) {
        const auto name = data_.shader.engine_type == "sky_texture" && socket.id == "tex_mapping.rotation"
                              ? "sky_rotation"
                              : socket.id.c_str();
        // Disable the complete tuple, without touching its authored
        // value/expression. This also restores it after disconnect or
        // Undo, and applies to dormant saved wires on hidden inputs.
        changed |= enableParm(name, !isConnected(i, false));
      }
      if (ch::native_shader_hidden(data_.shader.engine_type, socket.id).empty())
        continue;
      auto *input = getInputReference(i, true);
      input->setIsVisible(ch::native_shader_input_active(*this, socket.id, CHgetEvalTime()));
    }
    if (index_switch())
      for (int i = 0; i < index_count(); ++i)
        changed |= enableParm(("item" + std::to_string(i)).c_str(), !isConnected(i + 1, false));
    refreshing_ = false;
    return changed;
  }
  Definition &data_;
  Filter filter_;
  bool refreshing_{};
  std::string mix_type_{"color"};
  std::string pending_channels_, pending_ext_;
  bool has_pending_channels_{};
};
CyclesOperator::CyclesOperator(Definition &data)
    : VOP_Operator(
          data.operator_name.c_str(), data.label.c_str(), CyclesVop::construct, data.templates.data(),
          data.builder ? VOP_TABLE_NAME : nullptr, 0,
          data.shader.engine_type.starts_with("index_switch_") ? OP_MULTI_INPUT_MAX : unsigned(data.inputs.size()),
          "cycles", nullptr, OP_FLAG_GENERATOR | (data.builder ? OP_FLAG_NETWORK : 0), unsigned(data.outputs.size())),
      definition(data) {
  setOpTabSubMenuPath(("Cycles/" + data.category).c_str());
  setIconName(data.light_builder   ? "cycles_light_material"
              : data.world_builder ? "cycles_sky_world"
              : data.builder       ? "cycles_material_preview"
                                   : "VOP_shader");
  static_cast<VOP_OperatorInfo *>(getOpSpecificData())->setRenderMask("cycles");
  if (data.builder)
    static_cast<VOP_OperatorInfo *>(getOpSpecificData())->setShaderType(VOP_SURFACE_SHADER);
}
void register_color_ramp(OP_OperatorTable &table) {
  // Float Ramp was retired: Float Curve (an inline native ramp over Range)
  // replaces it. Only the Color Ramp is a dedicated ramp operator.
  constexpr bool color = true;
  auto data = std::make_unique<Definition>();
  data->operator_name = "cycles::color_ramp::1";
  data->shader.engine_type = "color_ramp";
  data->label = "Cycles Color Ramp";
  data->category = "Converter";
  data->help =
      "Map Factor through the Color ramp and the independent Alpha ramp. Native (Per Stop) samples each stop's Houdini "
      "basis into a 257-entry table. In RGB mode, Linear, Constant, Ease, B-Spline and Cardinal apply Blender's "
      "interpolation to both ramps; HSV/HSL blend Color along the Hue Interpolation path. For those modes the inline "
      "ramps preview Houdini's interpolation, not the rendered result. Color does not include alpha: connect Alpha "
      "separately to shader opacity when needed. Whole-ramp promotion carries both ramps, Color Mode and "
      "Interpolation. Unusable controls recover per output without removing geometry.";
  data->alpha_help = "Alpha output stops, independent of the Color stops. Default: opaque. Uses each stop's basis, "
                     "except that RGB Linear/Constant/Ease/B-Spline/Cardinal Interpolation applies to Alpha too.";
  ch::ShaderSocketDefinition factor;
  factor.id = "factor";
  factor.label = "Factor";
  factor.type = "float";
  factor.linkable = true;
  factor.authoring_default = .5f;
  data->shader.inputs.push_back(factor);
  if (color) {
    ch::ShaderSocketDefinition space;
    space.id = "ramp_color_space";
    space.label = "Color Mode";
    space.type = "enum";
    space.authoring_default = std::string("RGB");
    space.enum_values = {{"RGB", 0}, {"HSV", 1}, {"HSL", 3}, {"LAB", 2}};
    data->shader.inputs.push_back(space);
    ch::ShaderSocketDefinition hue;
    hue.id = "ramp_hue_interpolation";
    hue.label = "Hue Interpolation";
    hue.type = "enum";
    hue.authoring_default = std::string("NEAR");
    hue.enum_values = {{"NEAR", 0}, {"FAR", 1}, {"CW", 2}, {"CCW", 3}};
    data->shader.inputs.push_back(hue);
    ch::ShaderSocketDefinition interpolation;
    interpolation.id = "ramp_interpolation";
    interpolation.label = "Interpolation";
    interpolation.type = "enum";
    // Native for new nodes: the inline ramps then preview the render. HIP
    // files store every value, so existing scenes keep their own choice.
    interpolation.authoring_default = std::string("NATIVE");
    interpolation.enum_values = {{"LINEAR", 0},   {"CONSTANT", 1}, {"EASE", 2},
                                 {"B_SPLINE", 3}, {"CARDINAL", 4}, {"NATIVE", 5}};
    data->shader.inputs.push_back(interpolation);
  }
  ch::ShaderSocketDefinition result;
  result.id = color ? "color" : "value";
  result.label = color ? "Color" : "Value";
  result.type = color ? "color" : "float";
  data->shader.outputs.push_back(result);
  if (color) {
    ch::ShaderSocketDefinition alpha;
    alpha.id = "alpha";
    alpha.label = "Alpha";
    alpha.type = "float";
    data->shader.outputs.push_back(alpha);
  }
  parameters(*data);
  const auto type = color ? PRM_MULTITYPE_RAMP_RGB : PRM_MULTITYPE_RAMP_FLT;
  data->ramp_spare.setRampBasisDefault("linear");
  data->ramp_spare.addTokenValue(
      color ? PRM_SPARE_RAMP_COLOR_DEFAULT_TOKEN : PRM_SPARE_RAMP_FLOAT_DEFAULT_TOKEN,
      color ? "1pos ( 0 ) 1c ( 0 0 0 ) 1interp ( linear ) 2pos ( 1 ) 2c ( 1 1 1 ) 2interp ( linear )"
            : "1pos ( 0 ) 1value ( 0 ) 1interp ( linear ) 2pos ( 1 ) 2value ( 1 ) 2interp ( linear )");
  data->ramp_children.reset(PRMcreateRampTemplate("ramp", type, &data->ramp_spare, nullptr, nullptr));
  if (color) {
    // Inline native gadgets: an RGB Color ramp and an independent float
    // Alpha ramp. Bases are disabled where the render ignores them.
    data->ramp_name = PRM_Name("ramp", "Color");
    data->ramp_basis_disable =
        std::make_unique<PRM_Conditional>("{ ramp_color_space == HSV } { ramp_color_space == HSL } { ramp_color_space "
                                          "== RGB ramp_interpolation != NATIVE }",
                                          PRM_CONDTYPE_DISABLE);
    data->ramp_children.get()[PRM_RAMP_PARM_INTERP].setConditionalBasePtr(data->ramp_basis_disable.get());
    data->alpha_spare.setRampBasisDefault("linear");
    data->alpha_spare.addTokenValue(
        PRM_SPARE_RAMP_FLOAT_DEFAULT_TOKEN,
        "1pos ( 0 ) 1value ( 1 ) 1interp ( linear ) 2pos ( 1 ) 2value ( 1 ) 2interp ( linear )");
    data->alpha_children.reset(
        PRMcreateRampTemplate("alpha_ramp", PRM_MULTITYPE_RAMP_FLT, &data->alpha_spare, nullptr, nullptr));
    data->alpha_basis_disable = std::make_unique<PRM_Conditional>(
        "{ ramp_color_space == RGB ramp_interpolation != NATIVE }", PRM_CONDTYPE_DISABLE);
    data->alpha_children.get()[PRM_RAMP_PARM_INTERP].setConditionalBasePtr(data->alpha_basis_disable.get());
  }
  // Preserve the complete host interpolation menu. Removing enum entries
  // causes HOM ramp assignment to silently remap unknown bases. Extraction
  // evaluates the authored basis without silently remapping it.
  data->templates.pop_back();
  data->templates.emplace_back(type, data->ramp_children.get(), 1., &data->ramp_name, &data->ramp_size, nullptr,
                               &data->ramp_spare, data->help.c_str());
  if (color)
    data->templates.emplace_back(PRM_MULTITYPE_RAMP_FLT, data->alpha_children.get(), 1., &data->alpha_name,
                                 &data->alpha_size, nullptr, &data->alpha_spare, data->alpha_help.c_str());
  data->templates.emplace_back();
  table.addOperator(new CyclesOperator(*data));
  definitions().push_back(std::move(data));
}
} // namespace
namespace cycles_houdini {
namespace {
std::optional<bool> selector_activity(VOP_Node &node, const std::string &kind, const std::string &socket, double time,
                                      const std::map<std::string, std::string> &values = {}) {
  const auto integer = [&](const char *name) {
    if (const auto found = values.find(name); found != values.end())
      return std::stoi(found->second);
    return int(node.evalInt(name, 0, time));
  };
  if (kind.starts_with("switch_") && (socket == "false" || socket == "true")) {
    if (node.isConnected(0, false))
      return true;
    return (integer("switch") != 0) == (socket == "true");
  }
  if ((kind.starts_with("enable_output_") && socket == "value") || (kind == "warning" && socket == "message")) {
    if (node.isConnected(0, false))
      return true;
    return integer(kind == "warning" ? "show" : "enable") != 0;
  }
  if (kind.starts_with("index_switch_") && socket.starts_with("item") && socket != "item_count") {
    const auto token = socket.substr(4);
    if (token.empty() || token.find_first_not_of("0123456789") != std::string::npos)
      return false;
    const auto index = std::stoll(token);
    if (index >= integer("item_count"))
      return false;
    return node.isConnected(0, false) || index == integer("index");
  }
  return {};
}
} // namespace
bool native_shader_input_active(VOP_Node &node, const std::string &socket, double time) {
  const auto *op = dynamic_cast<CyclesOperator *>(node.getOperator());
  if (!op)
    return true;
  if (const auto active = selector_activity(node, op->definition.shader.engine_type, socket, time))
    return *active;
  if (op->definition.shader.engine_type == "mix_color" && (socket == "a" || socket == "b" || socket == "fac"))
    return true;
  return shader_ui_active(native_shader_hidden(op->definition.shader.engine_type, socket),
                          [&](const std::string &name) {
                            UT_String value;
                            node.evalString(value, name.c_str(), 0, time);
                            return value.toStdString();
                          });
}
std::map<std::string, bool> native_shader_input_activity(VOP_Node &node, double time,
                                                         const std::map<std::string, std::string> &values) {
  std::map<std::string, bool> result;
  const auto *op = dynamic_cast<CyclesOperator *>(node.getOperator());
  if (op)
    for (const auto &input : op->definition.shader.inputs)
      result.emplace(input.id, (op->definition.shader.engine_type == "mix_color" &&
                                (input.id == "a" || input.id == "b" || input.id == "fac")) ||
                                   shader_ui_active(native_shader_hidden(op->definition.shader.engine_type, input.id),
                                                    [&](const std::string &name) {
                                                      if (const auto found = values.find(name); found != values.end())
                                                        return found->second;
                                                      UT_String value;
                                                      node.evalString(value, name.c_str(), 0, time);
                                                      return value.toStdString();
                                                    }));
  if (op) {
    const auto &kind = op->definition.shader.engine_type;
    if (kind.starts_with("switch_"))
      for (const auto *socket : {"false", "true"})
        result[socket] = *selector_activity(node, kind, socket, time, values);
    if (kind.starts_with("enable_output_"))
      result["value"] = *selector_activity(node, kind, "value", time, values);
    if (kind == "warning")
      result["message"] = *selector_activity(node, kind, "message", time, values);
    if (kind.starts_with("index_switch_")) {
      const auto found = values.find("item_count");
      const int count =
          std::clamp(found == values.end() ? int(node.evalInt("item_count", 0, time)) : std::stoi(found->second), 0,
                     OP_MULTI_INPUT_MAX - 1);
      for (int i = 0; i < count; ++i) {
        const auto socket = "item" + std::to_string(i);
        result[socket] = *selector_activity(node, kind, socket, time, values);
      }
    }
  }
  return result;
}
std::vector<std::string> native_shader_input_controls(VOP_Node &node) {
  std::set<std::string> controls;
  if (const auto *op = dynamic_cast<CyclesOperator *>(node.getOperator())) {
    if (op->definition.shader.engine_type.starts_with("switch_"))
      controls.insert("switch");
    if (op->definition.shader.engine_type.starts_with("enable_output_"))
      controls.insert("enable");
    if (op->definition.shader.engine_type == "warning")
      controls.insert("show");
    if (op->definition.shader.engine_type.starts_with("index_switch_")) {
      controls.insert("index");
      controls.insert("item_count");
    }
  }
  if (const auto *op = dynamic_cast<CyclesOperator *>(node.getOperator()))
    for (const auto &input : op->definition.shader.inputs)
      for (const auto &clause : native_shader_hidden(op->definition.shader.engine_type, input.id))
        for (const auto &test : clause)
          controls.insert(test.parameter);
  return {controls.begin(), controls.end()};
}
bool is_native_shader_operator(const OP_Operator &op) { return dynamic_cast<const CyclesOperator *>(&op) != nullptr; }
bool is_cycles_ramp_operator(const OP_Operator &op) { return op.getName() == "cycles::color_ramp::1"; }
UT_Color native_shader_node_color(const std::string &category, const std::string &kind) {
  // Observed factory theme at Blender fbe6228777e7. Category assignments are
  // facts from its node registrations, including data-type-dependent classes.
  auto style = category;
  if (style == "Volume")
    style = "Shader";
  if (style == "Material" || style == "Network")
    style = "Group";
  if (kind == "vector_math" || kind == "vector_map_range" || kind == "mix_vector" || kind == "mix_vector_non_uniform" ||
      kind == "normal")
    style = "Vector";
  if (kind == "bevel")
    style = "Input";
  if (kind == "light_falloff")
    style = "Color";
  static const std::map<std::string, std::array<int, 3>> palette = {
      {"Input", {130, 53, 76}},     {"Output", {62, 35, 42}},  {"Shader", {43, 101, 43}},
      {"Texture", {121, 70, 29}},   {"Color", {110, 110, 35}}, {"Vector", {60, 60, 131}},
      {"Converter", {36, 98, 131}}, {"Group", {55, 71, 37}},   {"Interface", {29, 29, 29}}};
  const auto found = palette.find(style);
  const auto rgb = found == palette.end() ? std::array<int, 3>{48, 48, 48} : found->second;
  return UT_Color(UT_RGB, rgb[0] / 255., rgb[1] / 255., rgb[2] / 255.);
}
std::string native_shader_bypass_input(VOP_Node &node, const std::string &output) {
  auto kind = node.getOperator()->getName().toStdString();
  if (kind.starts_with("cycles::") && kind.ends_with("::1"))
    kind = kind.substr(8, kind.size() - 11);
  // Explicit primary data inputs. Factors, scale and range limits must not
  // accidentally become the bypass route merely because they share a type.
  static const std::map<std::string, std::string> primary = {{"add_closure", "closure1"},
                                                             {"mix_closure", "closure1"},
                                                             {"math", "value1"},
                                                             {"integer_math", "value1"},
                                                             {"boolean_math", "boolean1"},
                                                             {"map_range", "value"},
                                                             {"vector_map_range", "vector"},
                                                             {"clamp", "value"},
                                                             {"mix_color", "a"},
                                                             {"brightness_contrast", "color"},
                                                             {"gamma", "color"},
                                                             {"hsv", "color"},
                                                             {"invert", "color"},
                                                             {"rgb_to_bw", "color"},
                                                             {"separate_color", "color"},
                                                             {"separate_xyz", "vector"},
                                                             {"combine_color", "r"},
                                                             {"combine_xyz", "x"},
                                                             {"mapping", "vector"},
                                                             {"vector_rotate", "vector"},
                                                             {"vector_transform", "vector"},
                                                             {"bump", "normal"},
                                                             {"bevel", "normal"},
                                                             {"normal", "normal"},
                                                             {"normal_map", "strength"},
                                                             {"displacement", "normal"},
                                                             {"vector_displacement", "midlevel"},
                                                             {"blackbody", "temperature"},
                                                             {"wavelength", "wavelength"},
                                                             {"light_falloff", "strength"},
                                                             {"layer_weight", "blend"},
                                                             {"fresnel", "ior"},
                                                             {"wireframe", "size"},
                                                             {"brick_texture", "color1"},
                                                             {"checker_texture", "color1"},
                                                             {"gabor_texture", "scale"},
                                                             {"color_ramp", "factor"},
                                                             {"white_noise_texture", "vector"},
                                                             {"float_curve", "value"},
                                                             {"rgb_curves", "value"},
                                                             {"vector_curves", "value"}};
  std::string input;
  if (kind == "subnet") {
    const int index = node.getOutputFromName(UT_String(output.c_str()));
    if (index >= 0) {
      const auto type = node.getOutputType(index);
      const auto count = node.getNumVisibleInputs();
      for (unsigned n = 0; n < count; ++n) {
        UT_String candidate;
        node.getInputName(candidate, int(n));
        if (node.getInputType(int(n)) == type) {
          if (input.empty() || candidate == output.c_str())
            input = candidate.toStdString();
          if (candidate == output.c_str())
            break;
        }
      }
    }
  } else if (kind == "mix_color")
    input = "a";
  else if (kind.starts_with("switch_"))
    input = "false";
  else if (kind.starts_with("enable_output_"))
    input = "value";
  else if (kind.starts_with("index_switch_"))
    input = "item0";
  else if (kind == "vector_math") {
    if (output == "vector")
      input = "vector1";
  } else if (kind == "ambient_occlusion")
    input = output == "color" ? "color" : "distance";
  else if (const auto found = primary.find(kind); found != primary.end())
    input = found->second;
  if (!input.empty() && node.getInputFromName(UT_String(input.c_str())) >= 0)
    return input;
  return {};
}
} // namespace cycles_houdini
void newVopOperator(OP_OperatorTable *table) {
  const auto supported = ch::shader_graph_definitions();
  for (auto &shader : ch::shader_definitions()) {
    if (shader.engine_type == "owned_ramp")
      continue; // Private lowering target; native ramp controls author its source.
    if (shader.engine_type == "mix" || shader.engine_type == "mix_float" || shader.engine_type == "mix_vector" ||
        shader.engine_type == "mix_vector_non_uniform")
      continue;
    if (std::find(supported.begin(), supported.end(), shader.id) == supported.end())
      continue;
    auto data = std::make_unique<Definition>();
    data->shader = std::move(shader);
    const auto &authored = metadata.at(data->shader.engine_type);
    data->operator_name = "cycles::" + data->shader.engine_type + "::1";
    data->label = std::string("Cycles ") + authored.label;
    data->category = authored.category;
    data->help = authored.help;
    parameters(*data);
    table->addOperator(new CyclesOperator(*data));
    // New tiles use the artist-facing terminology too. Existing/custom
    // node names and the operator's saved type remain untouched.
    if (data->shader.engine_type == "mix_closure")
      table->setOpFirstName(data->operator_name.c_str(), "mix_shader");
    else if (data->shader.engine_type == "add_closure")
      table->setOpFirstName(data->operator_name.c_str(), "add_shader");
    else if (data->shader.engine_type == "mix_color")
      table->setOpFirstName(data->operator_name.c_str(), "mix");
    definitions().push_back(std::move(data));
  }
  auto output = std::make_unique<Definition>();
  output->operator_name = "cycles::surface_output::1";
  output->label = "Cycles Material Output";
  output->category = "Output";
  output->output = true;
  output->help = "Connect the surface shader and optionally an interior medium for a closed polygon mesh. Volume-only "
                 "sparse VDBs use Cycles Volume Material. Displacement retains its existing socket for saved networks.";
  ch::ShaderSocketDefinition surface;
  surface.id = "surface";
  surface.label = "Surface";
  surface.type = "closure";
  surface.linkable = true;
  output->inputs.push_back(surface);
  ch::ShaderSocketDefinition displacement;
  displacement.id = "displacement";
  displacement.label = "Displacement";
  displacement.type = "vector";
  displacement.linkable = true;
  output->inputs.push_back(displacement);
  auto medium = surface;
  medium.id = "volume";
  medium.label = "Volume";
  output->inputs.push_back(medium);
  output->templates.emplace_back();
  table->addOperator(new CyclesOperator(*output));
  definitions().push_back(std::move(output));
  auto volume_output = std::make_unique<Definition>();
  volume_output->operator_name = "cycles::volume_output::1";
  volume_output->label = "Cycles Volume Output";
  volume_output->category = "Output";
  volume_output->output = true;
  surface.id = "volume";
  surface.label = "Volume";
  volume_output->inputs.push_back(surface);
  volume_output->templates.emplace_back();
  table->addOperator(new CyclesOperator(*volume_output));
  definitions().push_back(std::move(volume_output));
  auto world_output = std::make_unique<Definition>();
  world_output->operator_name = "cycles::world_output::1";
  world_output->label = "Cycles World Output";
  world_output->category = "Output";
  world_output->output = true;
  surface.id = "surface";
  surface.label = "Background";
  world_output->inputs.push_back(surface);
  world_output->inputs.push_back(medium);
  world_output->help = "Connect a background and/or a world volume. World media use constant or procedural inputs; no "
                       "SOP grid fields are available.";
  world_output->templates.emplace_back();
  table->addOperator(new CyclesOperator(*world_output));
  definitions().push_back(std::move(world_output));
  auto light_output = std::make_unique<Definition>();
  light_output->operator_name = "cycles::light_output::1";
  light_output->label = "Cycles Light Output";
  light_output->category = "Output";
  light_output->output = true;
  surface.id = "surface";
  surface.label = "Surface";
  light_output->inputs.push_back(surface);
  light_output->help =
      "Connect Emission, optionally driven by textures, Blackbody, IES and color/utility nodes. The light's Color, "
      "Temperature, Power and Exposure multiply this emission. Analytic lights have no volume or displacement output.";
  light_output->templates.emplace_back();
  table->addOperator(new CyclesOperator(*light_output));
  definitions().push_back(std::move(light_output));
  auto builder = std::make_unique<Definition>();
  builder->operator_name = "cycles::material::1";
  builder->label = "Cycles Material";
  builder->category = "Material";
  builder->builder = true;
  ch::ShaderSocketDefinition method;
  method.id = "cycles_displacement_method";
  method.label = "Displacement Method";
  method.type = "enum";
  method.authoring_default = std::string("bump");
  method.enum_values = {{"bump", 0}, {"true", 1}, {"both", 2}};
  builder->shader.inputs.push_back(method);
  ch::ShaderSocketDefinition index;
  index.id = "cycles_pass_index";
  index.label = "Pass Index";
  index.type = "int";
  index.authoring_default = 0;
  builder->shader.inputs.push_back(index);
  // Material > Settings. Defaults are the engine's Shader defaults, which
  // every material used before these parameters existed.
  ch::ShaderSocketDefinition emission_sampling;
  emission_sampling.id = "cycles_emission_sampling";
  emission_sampling.label = "Emission Sampling";
  emission_sampling.type = "enum";
  emission_sampling.authoring_default = std::string("auto");
  emission_sampling.enum_values = {{"none", 0}, {"auto", 1}, {"front", 2}, {"back", 3}, {"front_back", 4}};
  builder->shader.inputs.push_back(emission_sampling);
  ch::ShaderSocketDefinition transparent_shadows;
  transparent_shadows.id = "cycles_transparent_shadows";
  transparent_shadows.label = "Transparent Shadows";
  transparent_shadows.type = "boolean";
  transparent_shadows.authoring_default = true;
  builder->shader.inputs.push_back(transparent_shadows);
  auto bump_map_correction = transparent_shadows;
  bump_map_correction.id = "cycles_bump_map_correction";
  bump_map_correction.label = "Bump Map Correction";
  builder->shader.inputs.push_back(bump_map_correction);
  ch::ShaderSocketDefinition volume_sampling;
  volume_sampling.id = "cycles_volume_sampling";
  volume_sampling.label = "Volume Sampling";
  volume_sampling.type = "enum";
  volume_sampling.authoring_default = std::string("multiple_importance");
  volume_sampling.enum_values = {{"distance", 0}, {"equiangular", 1}, {"multiple_importance", 2}};
  ch::ShaderSocketDefinition volume_interpolation;
  volume_interpolation.id = "cycles_volume_interpolation";
  volume_interpolation.label = "Volume Interpolation";
  volume_interpolation.type = "enum";
  volume_interpolation.authoring_default = std::string("linear");
  volume_interpolation.enum_values = {{"linear", 0}, {"cubic", 1}};
  ch::ShaderSocketDefinition volume_step_rate;
  volume_step_rate.id = "cycles_volume_step_rate";
  volume_step_rate.label = "Volume Step Rate";
  volume_step_rate.type = "float";
  volume_step_rate.authoring_default = 1.f;
  for (const auto *socket : {&volume_sampling, &volume_interpolation, &volume_step_rate})
    builder->shader.inputs.push_back(*socket);
  builder->help = "Bump affects shading; True changes mesh vertices; Both also retains bump detail. Motion-dependent "
                  "displacement requires per-shutter evaluation.";
  parameters(*builder);
  table->addOperator(new CyclesOperator(*builder));
  definitions().push_back(std::move(builder));
  auto volume_builder = std::make_unique<Definition>();
  volume_builder->operator_name = "cycles::volume_material::1";
  volume_builder->label = "Cycles Volume Material";
  volume_builder->category = "Material";
  volume_builder->builder = true;
  volume_builder->volume_builder = true;
  for (const auto *socket : {&volume_sampling, &volume_interpolation, &volume_step_rate})
    volume_builder->shader.inputs.push_back(*socket);
  parameters(*volume_builder);
  table->addOperator(new CyclesOperator(*volume_builder));
  definitions().push_back(std::move(volume_builder));
  auto world_builder = std::make_unique<Definition>();
  world_builder->operator_name = "cycles::world_material::1";
  world_builder->label = "Cycles World Material";
  world_builder->category = "Material";
  world_builder->builder = true;
  world_builder->world_builder = true;
  world_builder->templates.emplace_back();
  table->addOperator(new CyclesOperator(*world_builder));
  definitions().push_back(std::move(world_builder));
  auto light_builder = std::make_unique<Definition>();
  light_builder->operator_name = "cycles::light_material::1";
  light_builder->label = "Cycles Light Material";
  light_builder->category = "Material";
  light_builder->builder = true;
  light_builder->light_builder = true;
  light_builder->help =
      "Emission network for analytic Cycles lights. Assign this material under the light's Emission Material control. "
      "White Emission at Strength 1 preserves the light's Color, Temperature, Power and Exposure.";
  light_builder->templates.emplace_back();
  table->addOperator(new CyclesOperator(*light_builder));
  definitions().push_back(std::move(light_builder));
  register_color_ramp(*table);
}
