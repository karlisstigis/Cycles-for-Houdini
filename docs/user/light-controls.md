# Cycles light controls

Cycles Light provides **Use Temperature** and **Temperature** in Kelvin. Temperature multiplies Color, Power and Exposure, using the same Blackbody approximation and working-space conversion as Cycles. Leave Color white to use the temperature color alone. The range is 800–20000 K, matching Blender. Temperature is off by default. The optional Cycles tab for native Houdini lights has the same controls.

Area lights expose **Size** for Square and Disk. Rectangle and Ellipse also expose **Size Y**. The controls and resize handles set the full dimensions or diameters. Switching to a uniform shape keeps the stored second dimension for later reuse. Cycles Light follows Houdini's native light transform rules, including its unit-scale light transform under scaled parents. Use **Radius** to set a Point or Spot emitter's radius.

Lights do not blur over the shutter, as in Blender: with motion blur on, an animated or moving light renders at the requested frame. An emissive mesh does blur.

## Node-based lights

In `/mat`, create a **Cycles Light Material**. It contains a white **Cycles Emission**, at Strength 1, already connected to **Cycles Light Output → Surface**. Assign the builder path under the light's **Emission → Emission Material**. The default network preserves the light's brightness and tint.

The **Light Material** tool on the **Cycles Tools** shelf creates this setup. The **+** button beside an empty Emission Material field creates and assigns a material named after the light. Houdini adds a numerical suffix if that name is already used. Once assigned, **Open** replaces **+** and selects and frames the material in its `/mat` network. Opening does not change the assignment or create another material. Creation and assignment are one undoable action.

Blackbody, IES, textures, subnetworks and supported color/utility nodes can drive Emission. Light Color, Temperature, Power and Exposure multiply the shader. An assigned material overrides the light's Color Texture and IES Profile shortcuts. The light builder offers nodes supported by analytic lights; its output has no Volume or Displacement input.

## Portals

Turn on **Portal** on an area light (Blender: Light > Portal) and place it in a window or other opening, with its light direction pointing into the room. A portal emits nothing; it guides environment sampling through its shape, which cleans up interiors lit by the environment through openings. The mean brightness is unchanged. A portal keeps only its shape, size, position and direction (Reverse Direction), so Color, Power, Exposure, Normalize, Spread, Single Sided and the light's emission, linking, sampling, visibility and light-group settings are hidden while it is on. A portal is always one-sided. It needs an environment light or world to have any effect. The optional Cycles tab of native Houdini area lights has the same toggle; reinstall the tab on an older light to add it. Houdini's Mantra `env_portal` geometry and Karma's portal light prims are not read. Blender Link assets do not carry lights, so they carry no portals.
