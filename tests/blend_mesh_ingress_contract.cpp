// SPDX-License-Identifier: Apache-2.0
// Isolated native ingress contract. Link with blend_mesh.cpp and CPython; it
// requires neither Houdini nor Cycles and creates no host geometry or files.
#include "../src/houdini/blend_mesh.h"
#include <iostream>
#include <stdexcept>

namespace ch = cycles_houdini;
void require(bool value, const char *message) {
  if (!value)
    throw std::runtime_error(message);
}
int main() {
  Py_Initialize();
  try {
    const char *script = R"PY(
from array import array
arrays={
 'positions':array('f',[0,0,0, 1,0,0, 1,1,0, 0,1,0]),
 'edges':array('i',[0,1,1,2,2,3,3,0]),
 'polygon_counts':array('i',[4]),'vertex_indices':array('i',[0,1,2,3]),
 'material_indices':array('i',[1]),'smooth':array('B',[1]),
 'triangle_vertices':array('i',[0,1,2,0,2,3]),
 'triangle_loops':array('i',[0,1,2,0,2,3]),'triangle_polygons':array('i',[0,0]),
 'normals':array('f',[0,0,2]*4),'uv':array('f',[0,0,1,0,1,1,0,1]),
 'point':array('i',[2,3,5,7]),'face':array('f',[11]),
 'color':array('f',[.2,.3,.4,.5]*4),'edge_crease':array('f',[1,0,0,0]),
 'vert_crease':array('f',[0,1,0,0])}
record={'key':'quad','arrays':{name:name for name in arrays if name not in ('uv','point','face','color','edge_crease','vert_crease')},
 'normals_domain':'CORNER','default_uv':'UV Map','uv_layers':[{'name':'UV Map','native_name':'uv_map','array':'uv'}],
 'default_color':'paint','color_attributes':[{'name':'paint','array':'color','domain':'POINT'}],
 'attributes':[{'name':'point','array':'point','domain':'POINT','data_type':'INT'},
               {'name':'face','array':'face','domain':'FACE','data_type':'FLOAT'},
               {'name':'crease_edge','array':'edge_crease','domain':'EDGE','data_type':'FLOAT'},
               {'name':'crease_vert','array':'vert_crease','domain':'POINT','data_type':'FLOAT'}]}
)PY";
    require(PyRun_SimpleString(script) == 0, "Python mesh fixture failed");
    auto *globals = PyModule_GetDict(PyImport_AddModule("__main__"));
    auto *record = PyDict_GetItemString(globals, "record");
    auto *arrays = PyDict_GetItemString(globals, "arrays");
    const auto mesh = ch::capture_blend_mesh(record, arrays, {"/mat/a", "/mat/b"}, 21);
    require(mesh->positions.size() == 4 && mesh->triangles.size() == 2, "Mesh counts changed");
    require(mesh->triangles[0] == ch::Triangle{0, 1, 2} && mesh->triangles[1] == ch::Triangle{0, 2, 3},
            "Blender winding changed");
    require(mesh->triangle_material == std::vector<int>{1, 1}, "Face material slots changed");
    require(mesh->corner_normals.size() == 6 && mesh->corner_normals[0].z == 1, "Corner normals not normalized/mapped");
    require(mesh->corner_uv.size() == 6 && mesh->corner_uv[4] == ch::Float2{1, 1},
            "UV seams not mapped by triangle loop");
    const auto attr = [&](const char *name) -> const ch::NamedAttribute & {
      for (const auto &value : mesh->attributes)
        if (value.name == name)
          return value;
      throw std::runtime_error("Missing attribute");
    };
    require(attr("point").values[5].x == 7 && attr("face").values[5].x == 11,
            "Point/face attributes not mapped to corners");
    require(attr("uv_map").values[4] == ch::Float3{1, 1, 0}, "Encoded named UV not preserved");
    require(attr("__cycles_blender_alpha_paint").values[0].x == .5f, "Color alpha not preserved");
    require(attr("__cycles_blender_alpha___cycles_blender_default_color").values.size() == 6,
            "Default color alias missing");
    require(PyRun_SimpleString(R"PY(
class StoredDictionary(dict):
    def __getitem__(self,key):
        raise AssertionError('Native storage lookup must bypass subclass hooks')
stored_record=StoredDictionary(record)
stored_arrays=StoredDictionary(arrays)
)PY") == 0,
            "Dictionary-subclass fixture failed");
    const auto stored =
        ch::capture_blend_mesh(PyDict_GetItemString(globals, "stored_record"),
                               PyDict_GetItemString(globals, "stored_arrays"), {"/mat/a", "/mat/b"}, 31);
    require(stored->positions == mesh->positions && stored->triangles == mesh->triangles,
            "Dictionary storage lookup invoked subclass conversion hooks");
    auto capsule = ch::blend_mesh_capsule(mesh);
    require(capsule && ch::blend_mesh_from_capsule(capsule) == mesh, "Capsule ownership changed");
    capsule = {};
    // numpy's Windows int32 buffer format is 'l' (four-byte C long), not
    // array.array's 'i'. Exercise the real worker's contiguous 2-D buffers.
    require(PyRun_SimpleString(R"PY(
import numpy as np
arrays={key:np.asarray(value) for key,value in arrays.items()}
for key,width in [('positions',3),('edges',2),('triangle_vertices',3),('triangle_loops',3),('normals',3),('uv',2),('color',4)]:
    arrays[key]=arrays[key].reshape(-1,width)
)PY") == 0,
            "numpy buffer fixture failed");
    arrays = PyDict_GetItemString(globals, "arrays");
    const auto numpy_mesh = ch::capture_blend_mesh(record, arrays, {"/mat/a", "/mat/b"}, 25);
    require(numpy_mesh->triangles == mesh->triangles && numpy_mesh->corner_uv == mesh->corner_uv,
            "numpy typed buffers changed topology or UVs");
    const auto reused = ch::capture_blend_mesh(record, arrays, {"/mat/a", "/mat/b"}, 26, numpy_mesh);
    require(reused->triangles.storage_identity() == numpy_mesh->triangles.storage_identity(),
            "Matching triangle topology was duplicated");
    require(reused->corner_uv.storage_identity() == numpy_mesh->corner_uv.storage_identity(),
            "Unchanged UV storage was duplicated");
    require(reused->attributes[0].values.storage_identity() == numpy_mesh->attributes[0].values.storage_identity(),
            "Unchanged attribute storage was duplicated");
    require(PyRun_SimpleString("arrays['positions'].reshape(-1)[0]=.25") == 0, "Pose fixture mutation failed");
    const auto deformed = ch::capture_blend_mesh(record, arrays, {"/mat/a", "/mat/b"}, 27, numpy_mesh);
    require(deformed->positions.storage_identity() != numpy_mesh->positions.storage_identity(),
            "Changed pose reused stale positions");
    require(deformed->triangles.storage_identity() == numpy_mesh->triangles.storage_identity(),
            "Deformed pose duplicated unchanged topology");
    std::map<std::uintptr_t, std::size_t> unique_storage;
    for (const auto &[identity, bytes] : ch::blend_mesh_storage(*numpy_mesh))
      unique_storage[identity] = bytes;
    for (const auto &[identity, bytes] : ch::blend_mesh_storage(*deformed))
      unique_storage[identity] = bytes;
    std::size_t unique_bytes = 0;
    for (const auto &[identity, bytes] : unique_storage)
      unique_bytes += bytes;
    require(unique_bytes < ch::blend_mesh_nbytes(*numpy_mesh) + ch::blend_mesh_nbytes(*deformed),
            "Shared native buffers did not reduce retained bytes");
    require(ch::blend_mesh_nbytes(*numpy_mesh) >= numpy_mesh->positions.capacity() * sizeof(ch::Float3),
            "Native accounting omitted allocated positions");
    ch::MeshData repeated;
    repeated.positions = {{0, 0, 0}, {1, 0, 0}};
    repeated.corner_normals = repeated.positions;
    std::map<std::uintptr_t, std::size_t> repeated_storage;
    for (const auto &[identity, bytes] : ch::blend_mesh_storage(repeated))
      repeated_storage[identity] = bytes;
    std::size_t repeated_bytes = 0;
    for (const auto &[identity, bytes] : repeated_storage)
      repeated_bytes += bytes;
    require(repeated_bytes == ch::blend_mesh_nbytes(repeated), "Native byte count charged a shared allocation twice");
    require(PyRun_SimpleString("arrays={key:value.reshape(-1) for key,value in arrays.items()}") == 0,
            "numpy fixture flatten failed");
    arrays = PyDict_GetItemString(globals, "arrays");
    require(PyRun_SimpleString("arrays['positions'][0]=9; arrays['uv'][0]=9; "
                               "record['subdivision']={'type':'catmull_clark','level':2,'use_creases':True}") == 0,
            "Fixture mutation failed");
    require(mesh->positions[0].x == 0 && mesh->corner_uv[0].x == 0, "Mesh borrowed mutable Python buffers");
    const auto subd = ch::capture_blend_mesh(record, arrays, {"/mat/a", "/mat/b"}, 22);
    require(subd->subdivision && subd->triangles.empty() && subd->corner_normals.empty(),
            "Native subdivision became tessellated geometry");
    require(subd->subdivision->face_corners == std::vector<int>{0, 1, 2, 3} && subd->corner_uv.size() == 4,
            "Control mesh winding/corners changed");
    require(subd->subdivision->settings.max_level == 2 && !subd->subdivision->settings.adaptive,
            "Subdivision settings lost");
    require(subd->subdivision->edge_creases[0] == ch::EdgeCrease{0, 1, 10} &&
                subd->subdivision->vertex_creases[0] == ch::VertexCrease{1, 10},
            "Crease sharpness conversion failed");
    require(PyRun_SimpleString("del record['subdivision']; arrays['triangle_loops'][0]=3") == 0,
            "Fixture mutation failed");
    bool rejected = false;
    try {
      ch::capture_blend_mesh(record, arrays, {"/mat/a", "/mat/b"}, 23);
    } catch (const std::invalid_argument &) {
      rejected = true;
      PyErr_Clear();
    }
    require(rejected, "Inconsistent triangle loop topology was accepted");
    require(PyRun_SimpleString("arrays['triangle_loops'][0]=0; arrays['positions']=array('d',[0.]*12)") == 0,
            "Fixture mutation failed");
    rejected = false;
    try {
      ch::capture_blend_mesh(record, arrays, {"/mat/a", "/mat/b"}, 24);
    } catch (const std::invalid_argument &) {
      rejected = true;
      PyErr_Clear();
    }
    require(rejected, "Incorrect buffer dtype was accepted");
    // Failure must release every typed buffer before returning to Python.
    require(PyRun_SimpleString("arrays['positions']=array('f',[0.]*12); "
                               "arrays['triangle_loops'][0]=3") == 0,
            "Buffer lifetime fixture failed");
    rejected = false;
    try {
      ch::capture_blend_mesh(record, arrays, {"/mat/a", "/mat/b"}, 28);
    } catch (const std::invalid_argument &) {
      rejected = true;
    }
    require(rejected, "Invalid topology was accepted after typed-buffer conversion");
    require(PyRun_SimpleString("arrays['positions'].append(1.); arrays['positions'].pop(); "
                               "arrays['triangle_loops'][0]=0; "
                               "arrays['positions']=memoryview(arrays['positions'])[::2]") == 0,
            "Failed capture leaked a Python buffer export");
    rejected = false;
    try {
      ch::capture_blend_mesh(record, arrays, {"/mat/a", "/mat/b"}, 29);
    } catch (const std::invalid_argument &) {
      rejected = true;
    }
    require(rejected, "Noncontiguous position storage was accepted");
    auto *wrong_capsule = PyCapsule_New(const_cast<ch::MeshData *>(mesh.get()), "wrong.BlendMesh", nullptr);
    rejected = false;
    try {
      ch::blend_mesh_from_capsule(wrong_capsule);
    } catch (const std::invalid_argument &) {
      rejected = true;
    }
    Py_DECREF(wrong_capsule);
    require(rejected && !PyErr_Occurred(), "Wrong capsule type left a Python error or was accepted");
    // NumPy may use zero strides for every dimension of an empty export.
    // Such storage is C-contiguous and must preserve the old buffer contract.
    require(PyRun_SimpleString(R"PY(
empty_arrays={name:np.empty((0,3),dtype=np.float32) for name in ('positions','normals')}
empty_arrays.update({name:np.empty(0,dtype=np.int32) for name in ('polygon_counts','vertex_indices','material_indices','triangle_vertices','triangle_loops','triangle_polygons')})
empty_arrays['smooth']=np.empty(0,dtype=np.uint8)
empty_record={'key':'empty','arrays':{name:name for name in empty_arrays}}
)PY") == 0,
            "Empty typed-buffer fixture failed");
    const auto empty = ch::capture_blend_mesh(PyDict_GetItemString(globals, "empty_record"),
                                              PyDict_GetItemString(globals, "empty_arrays"), {}, 30);
    require(empty->positions.empty() && empty->triangles.empty(), "Empty contiguous typed buffers were rejected");
    std::cout << "Direct Blender mesh ingress contracts passed\n";
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    PyErr_Print();
    return 1;
  }
  Py_Finalize();
  return 0;
}
