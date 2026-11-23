# Persistent worker for interactive rendering

Houdini cooks the scene on its main thread. A separate worker owns the live Cycles scene and render device. Acknowledged generations allow the controller to discard obsolete edits, retain accepted geometry and recover from a worker crash. Disk rendering must preserve every requested frame. It has its own execution path using the same scene contract. [Architecture](../dev/renderer-architecture.md) · [Execution policy](../dev/notes/native-execution-policy.md)
