# Browse Blender asset libraries

The **Cycles Asset Browser** is a dockable panel that shows Blender asset
libraries as they are on disk: catalogs on the left, thumbnails in the middle,
details on the right, similar to Blender's Asset Browser. You can import
objects, collections and materials from it into Houdini.

## Install

The browser is optional. In Cycles Setup, select **Asset browser for Blender
asset libraries (dockable panel)**, listed under **Blender .blend asset
bridge**. The **Full** installation type selects both components. The browser
needs the bridge and a separate Blender installation, because Blender reads
the `.blend` files. Clearing the bridge also clears the browser. Run Setup
again to change either choice. A repair keeps the previous selection.

Without the component, Houdini gets no browser panel, button or menu entries.
Restart Houdini after Setup. Open the browser with **Asset Browser** on the
**Cycles Tools** shelf or in the **Cycles** main menu, or choose
**New Pane Tab Type > Cycles Asset Browser**.

### Try it from a development build

Run `./scripts/test-asset-browser.ps1` in PowerShell 7 from the project
folder. It rebuilds the development build (only changed parts), creates a small sample Blender asset library the
first time (needs Blender), and opens a separate Houdini with the browser
docked and the sample library registered. The installed renderer is not
touched. Libraries you add during these trials, the cache and the panel
settings are kept in `_work\asset-browser-trial` inside the project folder.

## Add libraries

Use the library menu at the top left to add a library folder. You can also
import the asset libraries from your Blender preferences: the browser asks
Blender for its configured libraries and adds any that are missing. Existing
names and settings are kept. The list is stored in
`<Houdini preferences>/cycles_houdini/asset_libraries.json`.

The first visit to a library reads every `.blend` file through Blender in the
background. After that, only changed files are read again. Without Blender,
the browser still shows what it has already read.

Catalogs come from the library's `blender_assets.cats.txt`. **All** shows every
asset and **Unassigned** shows assets without a catalog. Search matches names
and tags. Press **T** or **N** to show or hide the side panels. The two icon
buttons at the right end of the header open the **Options** menu (gear) and
the **Libraries...** editor (folder); hover a button to see its name.

## Import

- **Objects and collections** become a new geometry object containing a
  **Cycles Blender File** node (dropped into a geometry network, the node is
  created there). With **Link (reference)** the node is in **Link** mode: it
  renders the Blender meshes through the bridge; it is not a copy of the
  geometry, and it imports render meshes only. The viewport shows a convex
  hull by default (**Options > Imported Reference Viewport** switches to a
  bounding box). [Reference guide](blender-references.md) With the import
  mode **Convert & Append**, the node is in **Converted** mode: the asset is
  converted to Houdini geometry (packed primitives with appended, editable
  materials; **Load As** on the node unpacks it). **Options > Save Location**
  chooses where: **Save to Disk as .bgeo.sc** (default,
  `$HIP/assets_blender/<blend>/geo/<asset>/`) or **Stash in .hip** (inside the
  node, saved with the `.hip`, one frame). The node's **Update** converts the
  asset again after the `.blend` changed; **Cycles > Update All Blender Files**
  does it for every node whose source changed. See
  [The Cycles Blender File node](blender-import.md#the-cycles-blender-file-node).
- **Materials** are rebuilt as Cycles materials under `/mat`. A material is
  either translated completely or not at all. If something cannot be
  translated, the browser shows the reason instead of creating an incomplete
  material. **Assign to Selected Objects** imports the material and assigns it.
  The import mode menu decides how:
  - **Link (reference)**, the default, works like linked data in Blender.
    The scene gets one shared node per library material, and importing or
    assigning it again reuses that node. Its parameters are locked (greyed
    out), and its comment reads *Linked from <file>: <material>*.
  - **Convert & Append** creates a new, editable copy on every import.
- **Worlds** become an **Environment Light** in `/obj` when the world is a
  simple HDRI (an equirectangular image, optionally tinted, turned about the
  vertical axis by one Mapping node). The HDRI stays where the `.blend` points.
  Any other world becomes a **Cycles World Material** in `/mat`, which is set
  on the Cycles ROP. Link and Convert & Append work as for materials; a linked
  Environment Light keeps **Enable** editable.
  Cycles renders one world. When the scene already has environment lighting,
  the import asks first: **Replace** makes the new one active and disables
  the existing ones (they are not deleted), **Keep existing** imports the new
  one disabled, **Cancel** imports nothing.
- Other asset types, such as node groups, are shown but cannot be imported
  yet. The details panel says so.

Import with the **Import** button, a double-click or the context menu. The
import mode menu in the header alone decides between Link and Convert & Append.
Each import is one undo step. For objects and collections, Convert & Append converts the
asset into Houdini geometry files (packed primitives, appended materials) in the
background; see [Import collections and objects](blender-import.md#asset-browser-objects-and-collections).

### Linked materials and worlds

Right-click a linked material, Environment Light or World Material node and
open the **Cycles** submenu:

- **Make Editable** unlocks the parameters and removes the link. The
  node keeps its name and path, so assignments stay intact.
- **Reload from Library** exports the material from its `.blend` file again
  in the background. The rebuilt material replaces the old node under the same
  name and position, so assignments keep working. Each reload is one undo step.

Both items also act on every selected linked material or world. Inside a linked
material's own network, the network editor's selected actions apply to that
material. When a library file changes, the details panel shows it under
**In Scene** and offers **Reload from Library**.

Locked parameters are the practical read-only state. Houdini cannot stop you
from rewiring nodes inside the material network, and a reload discards such
edits. Use Make Editable to make a real local copy.

Drag and drop into other Houdini panes is an **experimental** setting and is
off by default. It has not been verified in every Houdini pane.

### The Cycles menus

These menus come with the Blender bridge component, with or without the asset
browser, because imports from **File > Import > Blender (.blend)** need them too.
Their items are greyed out when the Blender bridge is disabled.

The network editor's menu bar has a **Cycles** menu beside Labs:

| Item | Active in | Does |
|---|---|---|
| **Collect Textures for Selected** | material networks such as `/mat`, `/obj` | copies the textures of the selected materials, World Materials and Environment Lights into the project |
| **Collect Textures for All** | material networks, `/obj` | the same for the whole scene |
| **Update All Blender Files** | when a Blender File node or Blender Hierarchy object has the status "Source changed" | presses Update on each of them |
| **Relink Blender Assets...** | material networks, `/obj`, SOP networks | finds moved textures, geometry and `.blend` files |
| **Move Imported Assets into Project** | material networks, `/obj`, SOP networks | moves imports made before the scene was saved into the project |
| **Make Selected Editable** | material networks, with linked materials or worlds selected; `/obj`, with a linked Environment Light selected | as Make Editable above |
| **Reload Selected from Library** | as Make Selected Editable | as Reload from Library above |
| **Reload All Linked Assets** | material networks, when the scene has linked materials or worlds; `/obj`, when it has linked worlds | reloads every linked material and world |

The node right-click **Cycles** submenu has **Collect Textures**, **Make
Editable** and **Reload from Library**. It appears on materials, World
Materials, texture nodes and Environment Lights, and on linked materials and worlds.

### Collect Textures

Imported materials point at the textures where the `.blend` file finds them.
Collect Textures copies them next to your scene so the project no longer needs
the library:

- materials, worlds and lights imported from a `.blend` file go to
  `$HIP/assets_blender/<blend name>/tex/`. If the import record of such a
  material is lost, they go to `$HIP/assets_blender/_unknown_source/tex/` and
  the report says so;
- any other material goes to `$HIP/tex/<material name>/`.

Files keep their real names. A file that is already there with the same content
is reused, so a texture shared by several materials is stored once. A different
file with the same name gets `_2`, `_3`. All tiles of a UDIM set and all frames
of a sequence are copied, and the parameter keeps its `<UDIM>` or `$F4` token.
The parameters are rewritten to `$HIP/...` paths in one undo step. Textures
already inside the scene folder are left alone, and only the selected materials
are changed. Linked materials stay linked; only their texture paths change.

Save the scene first: Collect refuses to run on an unsaved scene. The report
lists files copied, reused and skipped, the total size, missing source files,
paths still in the user cache, and how many Blender File nodes in Link mode
(and old Blender Reference nodes) still read their `.blend` at render time:
switch them to **Converted** to make the project portable.

**Reload from Library after a collect** keeps the collected copy when the
library texture is unchanged, copies it again when it changed, and keeps the
copy with a warning when the library texture is gone.

### Relink Blender Assets

When a library or project folder moved, choose **Cycles > Relink Blender
Assets...** and pick the folder to search. Missing textures, geometry files
under `assets_blender` (also the converted file of a Blender File node) and the
**Blender File** paths of Blender File nodes, Blender Hierarchy objects and old
Blender Reference nodes are matched
by file name, ignoring case; a UDIM set or sequence matches when at least one of
its files is found. Where the import record knows the file's place inside the
library, that place is tried first. When several files match, you choose one;
the tool never guesses. A summary lists what was found and what was not before
anything changes. The rewrite is one undo step, and linked materials stay
linked. Very large folders are searched up to 200,000 files; the summary says
when the search stopped early.

### Move Imported Assets into Project

Imports into a scene that was never saved go to the user cache folder
(`assets_blender` inside the asset cache). After you save the scene, **Cycles >
Move Imported Assets into Project** moves the folders this scene uses into
`$HIP/assets_blender/` and rewrites the node and texture paths in one undo step
(including the converted files of Blender File nodes and Hierarchy objects).
After the first save of such a scene, Houdini offers this once.
If the project already has a folder for the same `.blend` file, the two are
merged: identical files are kept once, and different files are both kept, the
moved one renamed with `_2`. Files are never moved without this command.

## Files and caches

The browser treats libraries as **read-only**. It never writes into a library
folder unless you explicitly generate a Blender listing for a library that uses
the listing provider and confirm the prompt.

- **Thumbnails and metadata** are cached in
  `<Houdini preferences>/cycles_houdini/asset_cache`. Set
  `CYCLES_HOUDINI_ASSET_CACHE` to use another folder. Deleting the cache is
  safe; it is rebuilt on the next visit.
- **Textures** of imported materials are not copied: texture nodes point at
  the files the `.blend` points to, in the library. Only images packed inside
  the `.blend` are extracted, once, to `$HIP/assets_blender/<blend>/tex/` under
  their real file names. The import report says how many textures stay in the
  library, and a texture file that is missing is reported as a warning (the
  material is still imported). To make the project independent of the library,
  run **Cycles > Collect Textures**; a later **Reload from Library** keeps the
  collected copies. While the scene is still untitled, extracted files go to
  `<asset cache>/assets_blender` and the status line says so.
- **Re-importing** an object or collection whose files another saved scene in
  the same folder also uses asks first: *"<asset>" is also used by:
  shot_v01.hip. Replacing it changes that scene too.* **Replace** continues,
  **Cancel** changes nothing.

Materials are exported by Blender in the background. Houdini stays usable, the
status line shows progress, and **Cancel** stops the export.

**Open Blend File** opens the asset's source file in Blender.

If the panel says the browser is disabled, `CYCLES_HOUDINI_ASSET_BROWSER` is
set to `0` in Houdini's environment, for example by a studio package. Remove
that setting, or run Setup and select the component.
