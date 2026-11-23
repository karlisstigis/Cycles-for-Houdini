# Output formats and render information

## The extension selects the format

As in Mantra and Karma, the extension of the ROP's **Output Picture** selects the
image format. This applies to Render to Disk, background and farm renders
(`render_hip.py`) and frame ranges alike; `$F` expansion, pass folders, Existing
Files (Overwrite/Skip/Error) and the staged (atomic) file replacement work the same
way for every format.

| Extension | Bit depth | Alpha | Colour | Holds |
|---|---|---|---|---|
| `.exr` | 16-bit half or 32-bit float (**Precision**) | always (associated) | scene-linear working space, or **Override Color Space** | beauty and every pass, Cryptomatte, multipart |
| `.png` | 8-bit | when **Transparent Background** is on (straight alpha, as PNG requires) | display-referred: the OCIO default display and view, exactly the picture **Cycles Render** shows and saves with its default settings; with **Override Color Space**, that colour space | beauty only |
| `.jpg`, `.jpeg` | 8-bit, quality 100 | none: composited on black, like the viewer's JPEG save | as PNG | beauty only |
| `.tif`, `.tiff` | 32-bit float, LZW | when **Transparent Background** is on (associated) | scene-linear, like the EXR beauty, or **Override Color Space** | beauty only |

Other extensions are refused before rendering. The defaults follow what Mantra
and Karma write with no extra settings (Houdini 22.0.466): PNG 8-bit RGBA, JPEG
8-bit RGB at `vm_image_jpeg_quality` 100, TIFF 32-bit float RGBA with
`vm_image_tiff_compression` LZW. No parameters were added. Exposure and gamma of
the viewer are viewing adjustments and are not applied to disk renders.

## Passes with a PNG, JPEG or TIFF Output Picture

Passes, light groups, Cryptomatte and denoising data are EXR data. With a non-EXR
Output Picture, passes set to separate files are written to their own EXR files
exactly as configured (an automatic pass name keeps the picture's name and frame
field with the `.exr` extension, for example `shot.N.0001.exr` beside
`shot.0001.png`). A pass that would share the picture file (no Separate File, or
Multipart EXR on) stops the render before it starts with an error naming the pass;
nothing is dropped silently. Turn on **Passes to Separate Files** or use an `.exr`
Output Picture.

## Render information in the header

Every EXR written by a final render, beauty and pass files alike, carries these
entries in every part, besides the existing colour policy, denoiser, engine commit,
input diagnostics and Cryptomatte metadata. Pixel data, compression and the
part/channel layout are unchanged. Standard OpenEXR names are used where one exists,
Karma's names where Karma has one, and the `cycles_houdini:` prefix otherwise.

| Entry | Type | Meaning |
|---|---|---|
| `renderTime` | double | renderer wall-clock seconds, from the start of the frame's update in the renderer to the finished image; Houdini's scene export before it is not included (Karma/Mantra name) |
| `capDate`, `utcOffset` | string, float | local date and time of writing, `YYYY:MM:DD hh:mm:ss`; UTC = local time + `utcOffset` seconds (OpenEXR standard) |
| `cycles_houdini:samples_requested` | int | samples per pixel requested |
| `cycles_houdini:samples_rendered` | int | samples per pixel rendered (before adaptive per-pixel stopping) |
| `cycles_houdini:render_devices` | string | devices that rendered the frame, comma separated |
| `cycles_houdini:resolution` | int[2] | full image width and height; `pixelAspectRatio` holds the pixel aspect |
| `frame` | float | frame number (Karma name) |
| `framesPerSecond` | rational | scene frame rate (OpenEXR standard) |
| `cameraLabel` | string | camera object name (OpenEXR standard) |
| `cycles_houdini:camera` | string | camera node path, such as `/obj/cam1` |
| `pinholeFocalLength` | float | focal length in millimetres (OpenEXR standard) |
| `cycles_houdini:camera_aperture` | float | horizontal aperture (film width) in millimetres |
| `clipNear`, `clipFar` | float | camera clipping distances (Karma names) |
| `cycles_houdini:camera_projection` | string | camera projection |
| `cycles_houdini:motion_blur` | int | 1 when motion blur is on |
| `shutterAngle`, `expTime` | float | shutter in degrees and seconds, only with motion blur (OpenEXR standard) |
| `cycles_houdini:shutter_offset` | float | shutter offset, only with motion blur |
| `Software` | string | plugin build and Houdini version |
| `cycles_houdini:release_id` | string | plugin build (package id; `development` for a development build) |
| `cycles_houdini:houdini_version` | string | Houdini version |
| `cycles_houdini:hip_name` | string | HIP file name, without its folder |

Deliberately not written: the machine name, the user name and any full file path,
because frames are delivered to clients. One existing entry can still hold paths:
`cycles_houdini:source_diagnostics` names missing or unreadable inputs (for
example a texture path) when a render had such problems; it is empty (`[]`) for a
clean render. PNG files keep the text entries above as PNG text chunks; JPEG and
TIFF keep only their format's standard tags.
