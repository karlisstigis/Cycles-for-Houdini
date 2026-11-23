# Troubleshooting

## The Cycles ROP or shelf is missing

Local testing used Houdini Indie 22.0.466. Core and FX remain compatibility targets for this Windows package. Restart Houdini after Setup, then enable **Cycles Tools** in the shelf selector. If you installed Houdini after Cycles, run **Refresh Cycles registration.cmd** from the Cycles installation folder. A different Houdini build needs a compatible HDK API and runtime libraries. Setup lists the builds it can register.

## Houdini still sees Cycles after moving its folder

Use Windows **Settings > Apps > Installed apps** to uninstall the affected Cycles build, if its uninstaller is
still present. It can remove the owned registration after the renderer payload or Houdini is gone.

If you deleted the whole Cycles application folder and the uninstaller is gone, close Houdini. Open the Houdini
preferences folder you selected in Setup and find its `packages` folder. Open the matching Cycles JSON in a text
editor. If its `CYCLES_HOUDINI_HOST_ROOT` points to the missing installation, move just that JSON outside
`packages` as a backup. Leave other package files as they are and restart Houdini.

## The first image is empty or no GPU is available

Start with a lit sphere, a camera aimed at it and **CPU** selected on the Cycles ROP. In the **Cycles Render** panel, check the ROP and camera and click **Render**. If CPU works, check the ROP's device list for your NVIDIA OptiX device and select it. The viewer's **Memory** row reports Houdini and renderer process memory, with a separate figure for the renderer's usage on each selected GPU. Hover over it for the session's Cycles allocation counter.

## A Blender reference has a box in the viewport or fails at render time

The viewport box is the packed SOP's proxy. Use **Refresh Bounds** to measure the selection. For the render, enable **Blender .blend asset bridge** in Cycles Setup and install Blender. If discovery fails, set `CYCLES_HOUDINI_BLENDER` to the full path of `blender.exe`, then restart Houdini. Check that the file exists after `$HIP` expansion and that the object or collection name matches the Blender file exactly. Point instances use `f@index` for the Blender frame. [Packed reference guide](blender-references.md) · [Point instancing guide](point-instancing.md)

## Reporting a problem

If updates are unexpectedly slow, choose **Copy diagnostics** in the viewer's diagnostics menu (the info button at the lower right) while it happens and paste the text into your report. It lists open viewers, render sessions, renderer processes and the timing of the last 20 updates. The viewer's time counts from your action; hover over it to see how much was loading and how much was rendering.

Open **Collect diagnostics…** in the Cycles Render viewer. Review the preview before saving the ZIP. The default report omits private paths. The optional private details can include scene paths and worker log text. Enable them only if you intend to share those details. Include the action that reproduced the problem and the device you selected.

### Previous color configuration in IPR

When a color edit changes the working policy and the old and new configurations lack a common transform, the Cycles Render Panel shows the retained frame in raw color with a status message. MPlay keeps its last displayed image until a compatible frame arrives. Providing the OCIO roles `scene_linear` and `aces_interchange` in both configurations lets the viewer convert retained color pixels. Raw data passes and working-linear EXR output remain available.
