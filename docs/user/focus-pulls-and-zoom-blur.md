# Focus pulls and zoom blur

With a perspective camera, keyframe Houdini’s native **Focal Length** to animate a zoom.

For a focus pull, select the camera and choose **Cycles Tools → Add Camera Tab**.
Enable **Depth of Field** on the camera’s **Cycles** tab, choose an **F-Stop**, and
keyframe the native camera’s **Focus Distance**. The Cycles controls initially follow
the native camera’s **Focus Distance** and **F-Stop**.

To sample those changes during the shutter interval, enable **Motion Blur** under
**Rendering → Camera Effects** on the Cycles ROP. To see the effect within one frame,
render a frame where the chosen value changes during the shutter interval.

For supported projections and sampling details, see the [camera implementation notes](../dev/notes/native-binding-volume-camera.md).
