# SPDX-License-Identifier: Apache-2.0
"""Artist-facing shader help, shared by the offline Houdini helpcard generator.

Socket IDs are stable renderer IDs, not shortened panel labels (several panels
have a parameter called Weight). Keep semantics here; inspect the actual node
interface for parameter order, menu tokens, defaults and input/output names.
Existing, more specific native parameter help takes precedence over this table.
"""

BUILDERS = {
    "material": (
        "Cycles Material",
        "Build a surface shader for meshes, points and curves, with optional mesh displacement and an interior volume for a closed mesh.",
        "Assign this builder to the object or primitives with the Houdini material path. Dive inside and connect a surface shader, such as Principled BSDF, to Material Output / Surface. Connect a volume shader to Volume only for an interior medium; use Cycles Volume Material for sparse VDB objects. Connect Displacement or Vector Displacement to Displacement and choose the displacement method on the builder. True displacement needs sufficient tessellation; the Displacement node does not add mesh detail by itself.",
    ),
    "volume_material": (
        "Cycles Volume Material",
        "Build a volume shader for sparse VDB geometry.",
        "Assign this builder to the volume object and connect Principled Volume, Volume Scatter or Volume Absorption to Volume Output. Principled Volume can multiply Density by a named density grid. Simple scatter and absorption nodes need an explicit Attribute or Volume Info connection to read a field. Emission Strength needs an explicit field mask if it should follow sparse pyro. Check grid names and units when a volume appears empty or too dense.",
    ),
    "world_material": (
        "Cycles World Material",
        "Build environment illumination and an optional world medium.",
        "Connect Background to World Output / Background, then select this builder as the Cycles ROP world material. Drive Background Color from Environment Texture or Sky Texture for image or procedural lighting. Keep a sky Vector input unconnected to retain direct solar-disc sampling. A world volume surrounds the scene and accepts constants or procedural textures; it has no SOP volume grids. Background Strength changes illumination; display exposure changes how that illumination is viewed.",
    ),
    "light_material": (
        "Cycles Light Material",
        "Build a textured emission network for an analytic Cycles light.",
        "Connect Emission to Light Output and assign this builder to Emission Material on a Cycles Light. The result multiplies the light Color, Temperature, Power and Exposure. White Emission at Strength 1 leaves those controls unchanged. Use Image Texture, Blackbody, IES Texture and utility nodes to vary emission. An assigned material takes precedence over the light Color Texture and IES Profile shortcuts. Analytic light materials have no surface scattering, volume or displacement output.",
    ),
}

OUTPUTS = {
    "surface_output": (
        "Cycles Material Output",
        "Finish a surface material, optional closed-mesh interior medium and displacement.",
        "Connect a surface shader to Surface. Volume is an interior medium for a closed polygon mesh. Connect a vector from Displacement or Vector Displacement to Displacement, then select the method on the enclosing Cycles Material builder.",
    ),
    "volume_output": (
        "Cycles Volume Output",
        "Finish the volume shader for a sparse VDB object.",
        "Connect a volume shader to Volume. Density and emission must read the intended grids in the upstream network. A surface BSDF cannot be used as a volume shader.",
    ),
    "world_output": (
        "Cycles World Output",
        "Finish environment illumination and an optional world medium.",
        "Connect Background to Background. Volume accepts a world medium made from constants and procedural inputs; SOP volume grids are unavailable in world shading.",
    ),
    "light_output": (
        "Cycles Light Output",
        "Finish the emission network of an analytic Cycles light.",
        "Connect Emission to Surface. Its result multiplies Color, Temperature, Power and Exposure on the light. Assign the enclosing Cycles Light Material to the light Emission Material control.",
    ),
}

# Descriptions shared where the renderer socket has the same meaning. More
# context-sensitive cases appear in SPECIFIC below or parameter_description().
COMMON = {
    "roughness": "Width of the scattering lobe. Zero gives the sharpest response; larger values spread reflections or transmission and soften highlights.",
    "diffuse_roughness": "Roughness of the diffuse layer. Higher values change the diffuse response at grazing angles without blurring the specular layer.",
    "metallic": "Blend between a dielectric surface at 0 and a metal at 1. Metals reflect the base color and have no diffuse transmission.",
    "base_color": "Scene-linear surface color. For a dielectric this tints the diffuse layer; for a metal it colors reflection.",
    "color": "RGB value in the render working space. Values above 1 preserve high dynamic range; the display transform does not change this stored value.",
    "alpha": "Surface opacity: 0 is transparent and 1 is opaque. Intermediate values combine the material with transparent transmission.",
    "ior": "Index of refraction of the material. It controls dielectric Fresnel reflection and, where present, the bending of transmitted rays.",
    "IOR": "Index of refraction used for the dielectric Fresnel response. An unconnected Normal uses the surface shading normal.",
    "distribution": "Scattering distribution used to evaluate the lobe. Changing the model can alter highlight shape, energy and the interpretation of roughness.",
    "anisotropic": "Directional stretching of the specular lobe. Use Tangent and Rotation to orient the stretched highlight.",
    "anisotropy": "Directional scattering bias. For volumes, 0 scatters equally, positive values favor forward scattering and negative values favor backward scattering.",
    "anisotropic_rotation": "Orientation of the anisotropic highlight around the normal, in turns: 1 is a full revolution.",
    "tangent": "Direction used to orient anisotropic shading. Leave unconnected for the engine tangent; use Tangent for a chosen UV-derived direction.",
    "normal": "Shading normal direction. Connect Bump or Normal Map to alter shading without changing mesh positions. Leave unconnected to use the surface normal.",
    "specular_ior_level": "Scale of the dielectric specular response. The default 0.5 preserves the reflection determined by IOR; 0 removes that reflection.",
    "specular_tint": "Tint of dielectric specular reflection. White keeps a neutral Fresnel reflection.",
    "transmission_weight": "Weight of transmission through the surface. At 1 the dielectric base transmits rather than using diffuse shading; roughness and IOR still affect it.",
    "thin_wall": "Treat transmission as a thin sheet rather than a solid interior. Solid-medium dispersion and medium transitions require a closed solid surface.",
    "subsurface_weight": "Weight of subsurface scattering relative to the diffuse layer. Use Radius and Scale to set the travel distance beneath the surface.",
    "subsurface_radius": "Relative red, green and blue scattering distances, multiplied by Subsurface Scale. Larger red distance produces warmer light through thin features.",
    "subsurface_scale": "Overall scattering distance in Houdini scene units, multiplied by the RGB Radius.",
    "subsurface_ior": "Index of refraction at the subsurface boundary, used by the selected subsurface model.",
    "subsurface_anisotropy": "Preferred scattering direction within the subsurface medium. Positive values favor forward scattering; 0 is isotropic.",
    "subsurface_method": "Subsurface transport model. Available methods differ in accuracy, noise and behavior on thin or intersecting geometry.",
    "subsurface_roughness": "Roughness at the subsurface boundary, where supported by the selected transport model.",
    "coat_weight": "Weight of the clear top coating. Coat has its own roughness, IOR, tint and normal above the base layers.",
    "coat_roughness": "Width of reflections from the top coat. Lower values give sharper coating highlights.",
    "coat_ior": "Index of refraction of the clear top coat, controlling its Fresnel reflection.",
    "coat_tint": "Color transmitted through the coat to the layers beneath. White leaves the underlying color unchanged.",
    "coat": "Strength of the extra glossy coating on the hair fiber.",
    "sheen_weight": "Weight of the grazing-angle sheen layer, useful for fibers and fabric.",
    "sheen_roughness": "Width and softness of the sheen response at grazing angles.",
    "sheen_tint": "Color of the sheen layer, independent of the base surface color.",
    "thin_film_thickness": "Thickness of the interference film in nanometres. Zero disables the film; thickness and film IOR determine the interference colors.",
    "thin_film_ior": "Index of refraction of the thin interference film. Used together with its thickness in nanometres.",
    "emission_color": "Scene-linear emitted RGB, multiplied by Emission Strength. HDR values increase radiance and are preserved.",
    "emission_strength": "Multiplier for emitted radiance. In a volume, connect a field mask when emission should follow a named sparse grid.",
    "density": "Amount of participating medium per world-space distance. Increasing it makes the medium thicker and reduces the distance light travels.",
    "density_attribute": "Name of the volume grid that multiplies Density. An empty name uses Density alone; match the SOP grid name exactly.",
    "color_attribute": "Name of the color grid that modulates the volume color. An empty name leaves the constant color unchanged.",
    "temperature_attribute": "Name of the volume grid that multiplies the blackbody Temperature scale. Match the SOP grid name exactly.",
    "blackbody_intensity": "Strength of thermal emission from the temperature field. Zero disables blackbody emission.",
    "blackbody_tint": "RGB multiplier for thermal emission, after conversion from temperature into the render working space.",
    "absorption_color": "Color controlling light lost within the medium. Use with density and scattering to shape the transmitted spectrum.",
    "scatter_coeffs": "RGB scattering coefficient per unit distance: light redirected by the medium rather than absorbed.",
    "absorption_coeffs": "RGB absorption coefficient per unit distance: light removed from the ray by the medium.",
    "emission_coeffs": "RGB emitted radiance per unit distance along the ray through the medium.",
    "phase": "Phase-function model describing how a volume redirects incoming light. Directional parameters depend on this choice.",
    "g": "Mean directional bias of the phase function: 0 is isotropic, positive favors forward scattering and negative favors backward scattering.",
    "backscatter": "Relative contribution of the backward-scattering portion of the selected phase model.",
    "diameter": "Particle diameter used by the selected physical scattering model, in the units expected by that Cycles phase function.",
    "profile": "Shape profile used by the selected volume phase-function model.",
    "height": "Scalar height before subtracting Midlevel and applying Scale. A texture should use Data interpretation when its values represent height.",
    "midlevel": "Height considered undisplaced. Subtract this value from Height before multiplying by Scale.",
    "space": "Coordinate space in which the input is interpreted. Object follows local geometry; world remains fixed in the scene; tangent follows the surface basis.",
    "invert": "Reverse the direction of the effect while preserving its amount.",
    "convention": "Normal-map green-channel convention. OpenGL and DirectX store opposite Y directions; choose the convention used by the source image.",
    "strength": "Multiplier for the effect or emitted radiance. Zero removes the effect; values above 1 are allowed where supported.",
    "distance": "Distance in Houdini scene units over which the operation acts.",
    "radius": "Radius in Houdini scene units. Larger values extend the spatial reach of the effect.",
    "samples": "Number of extra rays used for this shading operation. More samples reduce noise at additional render cost.",
    "only_local": "Restrict traced intersections to the object being shaded instead of all eligible scene geometry.",
    "inside": "Evaluate occlusion from inside the surface instead of outside it.",
    "use_pixel_size": "Interpret Size in image pixels instead of world-space units, making the apparent line width depend on the render view.",
    "use_object_space": "Measure the distance in object coordinates instead of world coordinates.",
    "vector": "Coordinates or direction evaluated by this operation. Connect Texture Coordinate, Geometry, UV Map or Mapping to choose the space explicitly.",
    "w": "Additional scalar coordinate for 1D or 4D evaluation. Animate W to move through the procedural function without moving XYZ.",
    "dimensions": "Number of coordinates evaluated by the procedural function. W supplies 1D and the fourth coordinate in 4D modes.",
    "scale": "Frequency multiplier for the input coordinates. Higher values produce smaller repeated features.",
    "detail": "Number of fractal detail octaves. Higher values add finer features and increase evaluation cost; fractional values blend the last octave.",
    "detail_roughness": "Relative amplitude of successive detail octaves. Higher values make the fine detail stronger.",
    "detail_scale": "Coordinate frequency of the fine distortion detail relative to the base pattern.",
    "lacunarity": "Frequency multiplier between successive fractal octaves. Higher values separate the coarse and fine feature sizes more strongly.",
    "distortion": "Amount of coordinate warping applied before evaluating the pattern. Zero leaves the base pattern unwarped.",
    "use_normalize": "Normalize the fractal noise amplitude across its detail settings, instead of allowing amplitude to grow with added octaves.",
    "offset": "Offset controlling the placement or amplitude of the selected procedural pattern.",
    "gain": "Gain controlling the contribution of successive fractal layers.",
    "randomness": "Jitter of feature locations. Zero produces a regular arrangement; larger values make the cells less ordered.",
    "frequency": "Frequency of the procedural pattern in the input coordinate space.",
    "orientation_2d": "Gabor orientation angle in radians for 2D evaluation.",
    "orientation_3d": "Gabor orientation direction for 3D evaluation. Its direction determines the alignment of the pattern.",
    "feature": "Voronoi feature to evaluate, such as nearest point, smooth nearest distance or distance to an edge. Visible inputs and outputs depend on the feature.",
    "metric": "Distance metric between the shading coordinate and Voronoi feature points.",
    "exponent": "Exponent of the Minkowski distance metric. Used only when that metric is selected.",
    "smoothness": "Amount of smoothing between neighboring feature distances. Used by the Smooth F1 Voronoi feature.",
    "gradient_type": "Shape of the procedural gradient evaluated from the input coordinates.",
    "wave_type": "Choose planar bands or concentric rings for the wave pattern.",
    "bands_direction": "Axis or diagonal along which the wave bands repeat.",
    "rings_direction": "Axis around which the concentric wave rings are formed.",
    "wave_profile": "Shape of each repeating wave, such as sine, saw or triangle.",
    "phase_offset": "Shift along the repeating wave cycle. Animate it to move the pattern without changing its scale.",
    "filename": "Source image or photometric file. Houdini expressions resolve at render time; use an absolute path or a project-relative expression for portable scenes.",
    "colorspace": "Interpretation of source image values before shading. Auto follows Houdini OCIO and file metadata; Data preserves numeric values; Render working space means values are already scene-linear.",
    "interpolation": "Filtering between image texels. The choice affects sharpness and aliasing, rather than changing the source image resolution.",
    "projection": "Mapping from the input coordinates or direction to image coordinates. Choose the projection matching the source image.",
    "projection_blend": "Blend width between the axes of box projection. Zero produces hard transitions; larger values soften the seams.",
    "extension": "Behavior outside the image 0..1 coordinate range, such as repeating, clipping or extending edge pixels.",
    "alpha_type": "How source alpha is interpreted. Straight and premultiplied images store RGB differently; choose the source convention to avoid fringes.",
    "animated": "Mark the image source as time-varying so renderer image state can follow frame-dependent file expressions.",
    "missing": "RGB fallback used when the requested image data is unavailable. Check render diagnostics to identify the missing source.",
    "missing_alpha": "Alpha fallback used when the requested image data is unavailable.",
    "mapping_type": "Transform interpretation: Point includes translation, Vector ignores translation, Normal uses the inverse transpose, and Texture applies the inverse point mapping.",
    "location": "Translation of the coordinates in the selected mapping space.",
    "rotation": "Euler rotation in radians around the X, Y and Z axes.",
    "center": "Pivot about which the input coordinates are rotated.",
    "axis": "Axis of axis-angle rotation. Its direction determines the positive rotation orientation.",
    "angle": "Rotation angle in radians for the selected axis-angle mode.",
    "rotate_type": "Rotation representation: an arbitrary axis, a principal axis or Euler XYZ.",
    "transform_type": "Interpret the input as a point, vector or normal. Points include translation; normals use the inverse transpose.",
    "convert_from": "Coordinate space of the incoming point, vector or normal.",
    "convert_to": "Coordinate space in which to return the transformed point, vector or normal.",
    "direction_type": "Construct the tangent from UV coordinates or from a radial direction around an object axis.",
    "from_dupli": "Request instancer coordinates. This adapter does not provide instancer/reference-object coordinate data; see render diagnostics for recovery.",
    "attribute": "Name of the Houdini numeric field or volume grid to read. Use the exact field name; incompatible or missing data is reported during scene translation.",
    "layer_name": "Name of the color field to read. An empty name selects Cd. Numeric field values are used in the render working space.",
    "fac": "Blend weight. Zero gives the first or original value; one gives the second or processed value. Intermediate values interpolate the effect.",
    "fac_vector": "Independent X, Y and Z blend weights. Zero gives A and one gives B for the corresponding component.",
    "factor": "Blend weight between the original value and the processed result.",
    "a": "First operand. For a mix, Factor 0 returns A before any enabled result clamp.",
    "b": "Second operand. For a mix, Factor 1 returns B before any enabled result clamp.",
    "a_float": "Scalar A used in Float mode. Factor 0 returns this value.",
    "b_float": "Scalar B used in Float mode. Factor 1 returns this value.",
    "a_vector": "XYZ value A used in Vector mode. Factor 0 returns this value.",
    "b_vector": "XYZ value B used in Vector mode. Factor 1 returns this value.",
    "data_type": "Choose whether the Mix evaluates scalar, vector or RGB color values. The visible defaults and result type follow this selection.",
    "factor_mode": "Uniform uses one factor for XYZ; Non-Uniform uses a separate factor for each vector component.",
    "blend_type": "Color blend operation applied to A and B before blending by Factor. These operations evaluate scene-linear RGB values.",
    "use_clamp": "Limit the blend Factor or arithmetic result to 0..1, according to this node operation.",
    "use_clamp_result": "Clamp the resulting RGB values to 0..1 after blending. Disable this to preserve HDR or negative results.",
    "clamp": "Limit the mapped result to the destination interval instead of extrapolating outside it.",
    "math_type": "Operation applied to the inputs. The operation determines which operands are used and which controls remain visible.",
    "value": "Scalar operand or source value evaluated by this operation. Houdini expressions and animation are evaluated at render time.",
    "value1": "First scalar operand of the selected operation.",
    "value2": "Second scalar operand of the selected operation; hidden or unused for operations that require one input.",
    "value3": "Third scalar operand, used by operations such as Multiply Add, Compare or Smooth Minimum.",
    "vector1": "First XYZ operand of the selected vector operation.",
    "vector2": "Second XYZ operand; unused for operations requiring only one vector.",
    "vector3": "Third XYZ operand, used by three-input operations such as Multiply Add.",
    "boolean1": "First Boolean operand. Integer values greater than zero are true; zero and negative values are false.",
    "boolean2": "Second Boolean operand; used only by operations requiring two values.",
    "component": "Zero-based component index: 0 selects X, 1 selects Y and 2 selects Z.",
    "min": "Lower bound of the allowed range.",
    "max": "Upper bound of the allowed range.",
    "clamp_type": "Min Max uses the named bounds in order; Range also handles an inverted bound order.",
    "from_min": "Input value corresponding to the start of the destination range.",
    "from_max": "Input value corresponding to the end of the destination range.",
    "to_min": "Output value at From Min.",
    "to_max": "Output value at From Max.",
    "steps": "Number of discrete intervals used by Stepped interpolation.",
    "range_type": "Interpolation curve between the source and destination ranges.",
    "bright": "Brightness adjustment of the RGB values before the result is returned. This operates on scene-linear values.",
    "contrast": "Contrast adjustment around the Cycles formula midpoint. Strong values can create values below zero or above one.",
    "gamma": "Per-channel gamma exponent. This is a shader color operation, independent of source image color-space interpretation.",
    "hue": "Hue offset. The default 0.5 leaves hue unchanged; changing it rotates color around the hue wheel.",
    "saturation": "Saturation multiplier: 0 removes color, 1 preserves it, and values above 1 increase saturation.",
    "temperature": "Temperature in kelvin used for blackbody RGB. For Principled Volume this is a scale multiplied by the named temperature grid.",
    "wavelength": "Visible-light wavelength in nanometres, converted into RGB in the render working space.",
    "color_type": "Component representation used to combine or separate color: RGB, HSV or HSL.",
    "r": "Red component in RGB mode, or the first component of the selected color representation.",
    "g_color": "Green component in RGB mode, or the second component of the selected color representation.",
    "x": "X component of the assembled vector.",
    "y": "Y component of the assembled vector.",
    "z": "Z component of the assembled vector.",
    "switch": "Select False when zero and True when nonzero. Constant selectors prune the unused dependency branch.",
    "false": "Value returned when Switch is false. A constant true selector removes this branch from evaluation.",
    "true": "Value returned when Switch is true. A constant false selector removes this branch from evaluation.",
    "enable": "Enable the value or shader output. A constant disabled state returns zero, false or an empty shader and prunes the value branch.",
    "index": "Zero-based choice index. Out-of-range indices return zero, false or an empty shader rather than wrapping.",
    "item_count": "Number of choices. Choice indices start at zero; the shader graph node budget still applies to the flattened active network.",
    "show": "Emit the warning when this is a constant true value. Varying Show is unsupported and produces an information diagnostic.",
    "message": "Owned text emitted as a render diagnostic when Show is a constant true value.",
    "severity": "Diagnostic level assigned to the authored warning message.",
    "name": "Exact, case-sensitive output name. Enable the same named AOV on the Cycles ROP to record it.",
    "cycles_displacement_method": "Bump changes shading normals; True moves mesh vertices; Both moves vertices and retains bump detail. True displacement needs sufficient tessellation and motion-dependent displacement needs per-shutter evaluation.",
    "cycles_pass_index": "Material identifier, 0..32767. Enable Material Index on the Cycles ROP to record it. This does not alter shading or displacement.",
    "cycles_emission_sampling": "Light sampling of emissive surfaces (Blender: Emission Sampling). Auto picks Front, Back or both from the shader. None keeps the surface visible and still lighting the scene through bounces, but it is no longer a sampled light: use it for many small or faint emitters such as heavily instanced shapes.",
    "cycles_transparent_shadows": "Use transparent shadows when the material contains a Transparent BSDF. Off renders faster, but the material then casts an opaque shadow.",
    "cycles_bump_map_correction": "Apply corrections to solve shadow terminator artifacts caused by bump mapping.",
    "cycles_volume_sampling": "Sampling method for the material's volume: Distance, Equiangular or Multiple Importance. Multiple Importance suits most scenes; Equiangular helps near lights inside thin volumes.",
    "cycles_volume_interpolation": "Interpolation of volume grids. Cubic is smoother on low-resolution grids and slower.",
    "cycles_volume_step_rate": "Scales the distance between volume shader samples; lower is more accurate and slower. Used only when Biased Volume Ray Marching is on in the Cycles ROP.",
    "model": "Hair scattering model. Model selection changes which physical and artistic controls are available.",
    "parametrization": "Specify fiber absorption using direct RGB color, melanin concentration or an absorption coefficient.",
    "melanin": "Concentration of melanin pigment in the hair. Larger values absorb more light and darken the fiber.",
    "melanin_redness": "Balance of red pheomelanin and dark eumelanin when using melanin parametrization.",
    "tint": "Additional RGB tint applied to the hair color or absorption model.",
    "absorption_coefficient": "RGB absorption coefficient within the hair fiber. Larger values absorb more of that channel.",
    "absorption_coeffs_hair": "RGB absorption coefficient within the fiber.",
    "radial_roughness": "Roughness around the circumference of the hair, controlling the azimuthal scattering width.",
    "roughness_u": "Longitudinal hair roughness, spreading highlights along the strand direction.",
    "roughness_v": "Azimuthal hair roughness, spreading highlights around the fiber.",
    "random_roughness": "Amount of stable per-curve roughness variation around the specified roughness.",
    "random_color": "Amount of stable per-curve color or melanin variation.",
    "random": "Random seed value for fiber variation. A per-curve random signal keeps each strand consistent along its length.",
    "aspect_ratio": "Ellipticity of the fiber cross section for the selected hair model.",
    "R": "Weight of direct reflection from the hair surface.",
    "TT": "Weight of light transmitted through the hair fiber.",
    "TRT": "Weight of light transmitted into the fiber, internally reflected, then transmitted out.",
    "tilt": "Hair cuticle tilt angle in radians, controlling the shift between scattering lobes.",
    "offset_frequency": "Number of brick rows between applications of the row offset.",
    "squash_frequency": "Number of rows between applications of the brick width squash.",
    "squash": "Relative brick width on rows affected by Squash Frequency.",
    "brick_width": "Brick width in the scaled coordinate space.",
    "row_height": "Height of each brick row in the scaled coordinate space.",
    "mortar_size": "Width of the mortar gaps around the bricks.",
    "mortar_smooth": "Softness of the transition from brick color to mortar.",
    "mortar": "Scene-linear color used in the gaps between bricks.",
    "bias": "Bias toward one of the two brick colors in the procedural variation.",
    "depth": "Recursion depth of the Magic texture. Higher values introduce more layers of color detail.",
    "r_gon_sides": "Number of sides of each radial polygon cell.",
    "r_gon_roundness": "Roundness of the radial polygon corners.",
    "filter_width": "Filter width used to smooth the procedural boundary or repeated feature.",
    "length": "Maximum ray travel distance in Houdini scene units.",
    "direction": "Ray direction in world space. Its orientation determines where the operation traces or transports the ray.",
    "position": "Ray origin or destination in world-space Houdini scene units.",
}

SPECIFIC = {
    (
        "color",
        "value",
    ): "Constant scene-linear RGB output. HDR and negative component values are preserved; a display transform changes only the preview.",
    (
        "rgb_curves",
        "value",
    ): "RGB input transformed first by the Combined ramp and then by the corresponding R, G and B ramps. Factor blends with this original color.",
    (
        "vector_curves",
        "value",
    ): "XYZ input transformed independently by the X, Y and Z ramps over Range. Factor blends with this original vector.",
    (
        "gabor_texture",
        "type",
    ): "Evaluate a 2D or 3D Gabor pattern. The corresponding orientation control sets the kernel alignment.",
    (
        "subsurface_scattering",
        "method",
    ): "Subsurface transport model. Available methods differ in accuracy, noise and their treatment of thin or intersecting geometry.",
    (
        "texture_coordinate",
        "use_transform",
    ): "Request reference-object coordinates instead of the shaded object local coordinates. Reference-object transforms are unsupported by this adapter and produce a diagnostic with local recovery.",
    (
        "tangent",
        "axis",
    ): "Object axis around which the radial tangent is constructed. Used only in radial tangent mode.",
    ("normal", "direction"): "Reference direction for the Normal output and its dot product with the shading normal.",
    (
        "set_normal",
        "direction",
    ): "Direction to normalize and return as a shading normal. Connect the result to a shader Normal input.",
    ("vector_rotate", "invert"): "Apply the inverse of the selected rotation about Center.",
    (
        "brick_texture",
        "offset",
    ): "Horizontal shift of offset brick rows, as a fraction of brick width. Offset Frequency controls which rows receive it.",
    (
        "bump",
        "height",
    ): "Height signal whose derivatives perturb the normal. Connect a Data-interpreted height image or a procedural scalar. A constant height has no derivative and produces no bump.",
    (
        "bump",
        "distance",
    ): "Height-to-distance scale in Houdini scene units. Strength blends the perturbed normal; Distance sets the physical height scale.",
    ("bump", "strength"): "Blend from the original normal at 0 to the full bumped normal at 1.",
    (
        "displacement",
        "scale",
    ): "Distance multiplier applied to Height minus Midlevel. Object space follows local units; world space uses world distance.",
    (
        "vector_displacement",
        "scale",
    ): "Distance multiplier applied to the decoded XYZ displacement. True or Both on the material builder is required to move vertices.",
    (
        "vector_displacement",
        "midlevel",
    ): "Encoded component value representing zero displacement. Subtracted from the incoming vector before scaling.",
    (
        "normal_map",
        "color",
    ): "Encoded normal-map RGB. Read normal images with Data interpretation; a color-space transform would change the decoded directions.",
    ("normal_map", "strength"): "Amount of the decoded normal-map perturbation. Zero returns the unperturbed normal.",
    (
        "normal",
        "normal",
    ): "Reference normal for the Normal output and dot-product factor. Leave at its default to follow the engine normal convention.",
    (
        "principled_hair_bsdf",
        "offset",
    ): "Cuticle tilt angle in radians. It shifts the relative positions of the reflection and transmission highlights.",
    ("hair_bsdf", "offset"): "Angular offset of the hair scattering lobe in radians.",
    (
        "light_falloff",
        "smooth",
    ): "Smoothing distance for the near-light intensity singularity. Larger values soften the rapid intensity increase close to the light.",
    ("light_falloff", "strength"): "Base emission multiplier for the quadratic, linear and constant falloff outputs.",
    (
        "wireframe",
        "size",
    ): "Width of the edge mask, in world units or pixels according to Use Pixel Size. The mask follows tessellated triangles.",
    ("toon_bsdf", "size"): "Angular extent of the bright region of the toon response.",
    ("toon_bsdf", "smooth"): "Softness of the transition between the bright and dark toon regions.",
    ("layer_weight", "blend"): "Blend controlling the falloff of the Fresnel and Facing factors at grazing angles.",
    (
        "subsurface_scattering",
        "radius",
    ): "Relative red, green and blue scattering distances multiplied by Scale. Distances use Houdini scene units.",
    ("subsurface_scattering", "scale"): "Overall distance multiplier for the RGB subsurface scattering radius.",
    (
        "mapping",
        "scale",
    ): "XYZ coordinate scale. Point mapping applies scale before rotation and translation; Texture mapping uses the inverse transform.",
    ("vector_math", "scale"): "Scalar multiplier used by the selected vector operation, such as Scale or Refract.",
    ("wave_texture", "profile"): "Wave shape within each band or ring: sine, saw or triangle.",
    (
        "wave_texture",
        "phase",
    ): "Offset along the repeating wave cycle. Animate this to move the pattern without changing its scale.",
    (
        "noise_texture",
        "roughness",
    ): "Relative strength of successive fractal detail octaves. Higher values add stronger fine detail.",
    (
        "noise_texture",
        "type",
    ): "Fractal noise model. The selected model determines how octaves are combined and which inputs affect the result.",
    (
        "gabor_texture",
        "anisotropy",
    ): "Alignment of Gabor kernels around the selected orientation. Higher values create more directional structure.",
    (
        "metallic_bsdf",
        "fresnel_type",
    ): "Artistic uses base color and edge tint; Physical uses complex RGB IOR and extinction coefficient.",
    (
        "metallic_bsdf",
        "ior",
    ): "RGB real part of the metal complex index of refraction. Used with the RGB extinction coefficient in Physical mode.",
    (
        "metallic_bsdf",
        "k",
    ): "RGB extinction coefficient, the imaginary part of a conductor complex IOR. Used in Physical mode.",
    ("metallic_bsdf", "edge_tint"): "Artistic color of the metal reflection at grazing angles.",
    (
        "bevel",
        "radius",
    ): "Radius of the shader bevel in Houdini scene units. This changes the normal through extra rays and does not change geometry.",
    (
        "ambient_occlusion",
        "distance",
    ): "Maximum distance of occlusion rays in world units. Nearby geometry within this range can darken the result.",
    ("ray_portal_bsdf", "color"): "RGB multiplier for radiance transported through the ray portal.",
    (
        "ies_light",
        "filename",
    ): "IES photometric profile with its forward axis along local -Z. Connect Fac to Emission Strength. Only TILT=NONE profiles are supported.",
    (
        "vertex_color",
        "layer_name",
    ): "Name of the numeric color field. Empty selects Cd. Compatible deformation samples are read over the shutter.",
    ("combine_color", "g"): "Green in RGB mode, saturation in HSV or HSL mode.",
    ("combine_color", "b"): "Blue in RGB mode, value in HSV mode, or lightness in HSL mode.",
    ("combine_color", "r"): "Red in RGB mode, hue in HSV or HSL mode.",
    (
        "sky_texture",
        "sky_type",
    ): "Atmospheric model. Modern single/multiple-scattering models use altitude and atmospheric densities; legacy models use a direction and turbidity.",
    (
        "sky_texture",
        "sun_disc",
    ): "Include the bright solar disc. Leave Vector unconnected so Cycles can sample the solar disc directly.",
    (
        "sky_texture",
        "sun_intensity",
    ): "Multiplier for the solar-disc radiance. Sky atmosphere illumination remains controlled by the selected model.",
    (
        "sky_texture",
        "air_density",
    ): "Relative density of air molecules controlling Rayleigh scattering. The standard atmosphere uses 1.",
    (
        "sky_texture",
        "aerosol_density",
    ): "Relative aerosol density controlling atmospheric haze and Mie scattering. Larger values produce a hazier sky.",
    ("sky_texture", "ozone_density"): "Relative ozone density affecting spectral absorption through the atmosphere.",
    (
        "sky_texture",
        "turbidity",
    ): "Amount of atmospheric haze in the legacy sky models. Higher values make the sky hazier.",
    (
        "sky_texture",
        "ground_albedo",
    ): "Fraction of light reflected by the ground into the atmosphere in the Hosek/Wilkie sky model.",
    (
        "color_ramp",
        "fac",
    ): "Position at which to look up the Color and Alpha ramps. Zero samples their left ends and one their right ends.",
    (
        "color_ramp",
        "factor",
    ): "Position at which to look up the Color and Alpha ramps. Zero samples their left ends and one their right ends.",
    (
        "color_ramp",
        "ramp",
    ): "RGB color stops sampled by Factor. Alpha is authored on the separate Alpha ramp and returned as a separate output.",
    (
        "color_ramp",
        "alpha_ramp",
    ): "Opacity stops sampled by Factor independently of the Color stops. Connect the Alpha output to material opacity when needed.",
}


def parameter_description(kind, name, existing="", summary=""):
    """Return a semantic description and its origin for the coverage report."""
    # Native specialized explanations include coordinate conventions and
    # renderer limitations. Discard only the old repeated whole-node synopsis.
    if existing and existing.strip() != summary.strip():
        return existing.strip(), "native"
    if (kind, name) in SPECIFIC:
        return SPECIFIC[kind, name], "specific"
    if name.startswith("curve_"):
        if name == "curve_domain":
            return (
                "Input domain of the ramps. Ramp position 0 is the range start and 1 is its end; key values are output values and can be HDR or negative.",
                "specific",
            )
        if name == "curve_extension":
            return (
                "Behavior outside the ramp domain. Extrapolated continues the end slopes; Horizontal holds the first and last values.",
                "specific",
            )
        return (
            "Output values of this channel as an inline native float ramp. Positions span Range; each key basis controls the interpolation shown by the ramp.",
            "specific",
        )
    if name.startswith("item") and name[4:].replace("#", "").isdigit():
        return (
            "Choice value at this zero-based index. A constant Index evaluates only the selected branch; out-of-range indices return zero or an empty shader.",
            "specific",
        )
    if name.startswith("item") and "#" in name:
        return (
            "Choice value in the ordered list. Index 0 selects the first choice; constant selection prunes unused branches.",
            "specific",
        )
    if name in ("color1", "color2"):
        if kind == "brick_texture":
            return (
                ("First" if name == "color1" else "Second")
                + " brick color. The procedural brick variation blends these colors; mortar has its own color.",
                "specific",
            )
        if kind == "checker_texture":
            return (
                "First" if name == "color1" else "Second"
            ) + " alternating checker color in the render working space.", "specific"
        return (
            "First" if name == "color1" else "Second"
        ) + " scene-linear RGB input to the selected color operation.", "specific"
    if name == "value" and kind.startswith("enable_output_"):
        return (
            "Value returned when Enable is true. A constant disabled state prunes this branch and returns zero, false or an empty shader.",
            "specific",
        )
    if name == "value" and kind == "hsv":
        return "Brightness multiplier in the HSV adjustment. One leaves the original value unchanged.", "specific"
    if name in COMMON:
        return COMMON[name], "common"
    return "", "missing"


LIGHT_OVERVIEW = """Create a Point, Spot, Area or Sun source with one Cycles Light object. The named object appears in the Houdini viewport and Light Linker. Enter the node state to position and aim the source, then drag the native size, radius or cone handles.

For a soft area key, choose Area, set the shape and full dimensions in scene units, leave Normalize on, then adjust Power and Exposure. Point and Spot radius is a world-space distance. Sun Angle is an angular diameter in degrees.

Color, optional blackbody Temperature, Power and Exposure multiply the emitted light. Normalize keeps total power constant as the source size changes. Point, Spot and Area use watts when normalized; normalized Sun uses irradiance in watts per square metre. Exposure adds stops, multiplying power by 2 raised to Exposure.

Color Texture accepts a file or an owned op: image. Area sources map their full dimensions to 0..1; other light types use local outgoing equirectangular directions. An IES profile uses local -Z and requires TILT=NONE. Its file-derived radiometric scale is preserved, with Power acting as a multiplier. Assign a Cycles Light Material for an authored emission network; this takes precedence over the image and IES shortcuts. Missing or invalid sources retain the base light and produce diagnostics. Use Cycles IPR to preview textured or IES illumination.

Use Light Linking and Shadow Linking to select illuminated objects and blockers. Ray Visibility controls which ray types see the emitter. A Light Group records this source in a named RGB pass when the same group is enabled on the Cycles ROP; unregistered groups still illuminate Beauty. Shadow Catcher Reference distinguishes real/reference lights from added virtual lights for the Shadow Catcher calculation.

Use a native Environment Light or a Cycles World Material for environment illumination. Analytic-light motion blur and light portals require additional adapters."""

LIGHT_PARAMETERS = {
    "light_enable": "Include this light in Cycles renders and its native viewport approximation.",
    "cycles_light_color": "Scene-linear RGB tint, multiplied by optional blackbody Temperature, emission texture or material, Power and Exposure. White preserves those other colors.",
    "cycles_light_area_shape": "Emitter outline: Square and Disk use Size for both dimensions; Rectangle and Ellipse also use Size Y.",
    "singlesided": "Emit from one side of the area source. Disable to emit from both sides; direction arrows in the viewport show the active sides.",
    "reverse": "Reverse the emitting side of a single-sided area source. Local -Z is the ordinary forward direction.",
    "cycles_light_max_bounces": "Maximum path depth at which this light contributes. Lower values restrict indirect illumination from this source.",
    "cycles_light_cast_shadow": "Allow scene blockers to shadow this light. Disabling removes this source shadowing while retaining illumination.",
    "cycles_light_use_mis": "Combine direct-light and shader sampling using multiple importance sampling. Leave enabled for efficient sampling of most lights.",
    "cycles_light_is_sphere": "Use a spherical point/spot source instead of the disk approximation. Radius controls the extent of the source.",
    "cycles_light_visibility_camera": "Show the emitter surface to primary camera rays. This controls the source appearance, not whether it lights the scene.",
    "cycles_light_visibility_diffuse": "Make the emitter visible to diffuse scattering rays.",
    "cycles_light_visibility_glossy": "Make the emitter visible in glossy and specular reflection paths.",
    "cycles_light_visibility_transmission": "Make the emitter visible through transmissive scattering paths.",
    "cycles_light_visibility_volume": "Make the emitter visible to volume scattering paths.",
    "t": "Local translation of the light relative to its parent, in Houdini scene units.",
    "r": "Local Euler rotation of the light in degrees. Analytic lights aim along their local -Z axis.",
    "s": "Native light scale channels. Use the Cycles Size or Radius control to change the emitter dimensions.",
    "p": "Pivot position used for rotation and scale in the local transform.",
    "pr": "Pivot orientation in degrees, applied as part of the Houdini object transform.",
    "scale": "Native light scale channel. Use the Cycles Size or Radius control to change the emitter dimensions.",
    "xOrd": "Order in which Houdini composes object scale, rotation and translation.",
    "rOrd": "Order of the Euler rotations used by the object transform.",
    "pre_xform": "Transform pre-applied before the editable local transform.",
    "keeppos": "Preserve the world-space light transform when changing its input parent.",
    "lookatpath": "Object whose origin the light aims toward using the Houdini Look At constraint.",
    "lookup": "Up-vector convention used to resolve the light orientation when aiming at another object.",
    "up": "Explicit up-vector used by the Houdini aim constraint.",
    "pathobjpath": "Curve object used by the Houdini path constraint.",
    "roll": "Rotation about the light aim axis when using a path constraint, in degrees.",
    "pos": "Position along the constraint path.",
    "pathorient": "Orient the light along the tangent of its constraint path.",
    "upvector": "Up-vector for orienting the light along its constraint path.",
    "bank": "Bank the light as it follows its constraint path.",
    "additive": "Combine the path constraint with the editable local transform.",
    "constraints_on": "Evaluate the Houdini object constraint network when computing the light transform.",
    "constraints_path": "Constraint network used to compute the light object transform.",
    "cacheinput": "Cache the input parent transform according to the native Houdini object evaluation policy.",
    "childcomp": "Compensate child object transforms when changing this light object transform, according to the native Houdini parenting behavior.",
}


def visible_parameters(group):
    """Flatten artist-facing controls, retaining controls hidden by conditions."""
    import hou

    result = []

    def visit(entries, hidden=False):
        for parm in entries:
            if isinstance(parm, hou.FolderParmTemplate):
                if parm.folderType() in (
                    hou.folderType.MultiparmBlock,
                    hou.folderType.ScrollingMultiparmBlock,
                    hou.folderType.TabbedMultiparmBlock,
                ):
                    if not hidden and not parm.isHidden():
                        result.append(parm)
                        visit(parm.parmTemplates())
                else:
                    visit(parm.parmTemplates(), hidden or parm.isHidden())
            elif (
                not hidden
                and not parm.isHidden()
                and parm.type() not in (hou.parmTemplateType.Label, hou.parmTemplateType.Separator)
            ):
                result.append(parm)

    visit(group.entries())
    return result


def light_help_card(group):
    """The same formatted Help section is used in the HDA and external page."""
    lines = [
        "= Cycles Light =",
        "#type: node",
        "#context: obj",
        "#namespace: cycles",
        "#internal: light",
        "#version: 1",
        "",
        '"""Create an analytic point, spot, area or sun light with Cycles emission, linking and visibility controls."""',
        "",
    ]
    lines.extend(LIGHT_OVERVIEW.splitlines())
    lines.extend(["", "@parameters", ""])
    documented, missing = [], []
    for parm in visible_parameters(group):
        description = LIGHT_PARAMETERS.get(parm.name()) or parm.help()
        if not description:
            missing.append({"type": "cycles::light::1", "name": parm.name(), "label": parm.label()})
            continue
        lines.extend([parm.label().replace(":", " ") + ":", "    #id: " + parm.name(), ""])
        lines.extend("    " + paragraph for paragraph in description.splitlines())
        lines.append("")
        documented.append(
            {
                "name": parm.name(),
                "label": parm.label(),
                "origin": "light" if parm.name() in LIGHT_PARAMETERS else "native",
            }
        )
    lines.extend(
        [
            "== See also ==",
            "",
            "[Cycles Light Material|/nodes/vop/cycles--light_material-1]",
            "",
            "[Cycles shader guide|/cycles/shaders]",
            "",
        ]
    )
    return "\n".join(lines), documented, missing
