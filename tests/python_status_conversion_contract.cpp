// SPDX-License-Identifier: Apache-2.0
// Standalone baseline/new CPython runners; no renderer, HDK or artist runtime.
#include "../src/houdini/python_status.h"
#include <iostream>
#include <stdexcept>
#include <limits>
namespace ch = cycles_houdini;
namespace cycles_houdini {
Capabilities test_caps;
Capabilities capabilities() { return test_caps; }
std::vector<std::string> surface_graph_definitions() { return {"surface/a", "surface/\u00e9"}; }
std::vector<std::string> volume_graph_definitions() { return {"volume/a"}; }
std::vector<std::string> world_graph_definitions() { return {}; }
std::vector<std::string> light_graph_definitions() { return {"light/a", "light/b"}; }
} // namespace cycles_houdini
namespace legacy {
struct Ref {
  PyObject *object{};
  ~Ref() { Py_XDECREF(object); }
  operator PyObject *() const { return object; }
};
PyObject *failure(const std::exception &e) {
  PyErr_Clear();
  PyErr_SetString(PyExc_RuntimeError, e.what());
  return nullptr;
}
#include "fixtures/python_status_legacy.inc"
} // namespace legacy
int main(int argc, char **argv) {
  Py_Initialize();
  const bool old = argc > 1 && std::string(argv[1]) == "baseline";
  auto *globals = PyModule_GetDict(PyImport_AddModule("__main__"));
  PyRun_SimpleString("import json\nrows=[]\ndef typed(value):\n if isinstance(value,dict): return "
                     "{key:typed(value[key]) for key in sorted(value)}\n if isinstance(value,list): return "
                     "['list',[typed(item) for item in value]]\n return [type(value).__name__,value]\n");
  for (int fixture = 0; fixture < 12; ++fixture) {
    ch::SessionStatus s;
    ch::PythonStatusExtraction a;
    s.generation = (fixture == 0 ? 0ULL : fixture == 1 ? std::numeric_limits<unsigned long long>::max() : 1ULL);
    s.progress = (fixture == 0 ? 0.0 : 2.125);
    s.elapsed_seconds = (fixture == 0 ? 0.0 : 3.125);
    s.first_image_seconds = (fixture == 0 ? 0.0 : 4.125);
    s.geometry_conversions = (fixture == 0   ? 0ULL
                              : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                             : 5ULL);
    s.object_updates = (fixture == 0 ? 0ULL : fixture == 1 ? std::numeric_limits<unsigned long long>::max() : 6ULL);
    s.owned_geometry_bytes = (fixture == 0   ? 0ULL
                              : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                             : 7ULL);
    s.device_allocation_bytes = (fixture == 0   ? 0ULL
                                 : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                                : 8ULL);
    s.device_allocation_peak_bytes = (fixture == 0   ? 0ULL
                                      : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                                     : 9ULL);
    a.cooking_seconds = (fixture == 0 ? 0.0 : 10.125);
    a.conversion_seconds = (fixture == 0 ? 0.0 : 11.125);
    a.extractions = (fixture == 0 ? 0ULL : fixture == 1 ? std::numeric_limits<unsigned long long>::max() : 12ULL);
    a.cache_hits = (fixture == 0 ? 0ULL : fixture == 1 ? std::numeric_limits<unsigned long long>::max() : 13ULL);
    s.geometry_resources = (fixture == 0   ? 0ULL
                            : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                           : 14ULL);
    s.object_resources = (fixture == 0 ? 0ULL : fixture == 1 ? std::numeric_limits<unsigned long long>::max() : 15ULL);
    s.material_resources = (fixture == 0   ? 0ULL
                            : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                           : 16ULL);
    s.light_resources = (fixture == 0 ? 0ULL : fixture == 1 ? std::numeric_limits<unsigned long long>::max() : 17ULL);
    a.packed_instances = (fixture == 0 ? 0ULL : fixture == 1 ? std::numeric_limits<unsigned long long>::max() : 18ULL);
    a.index_instance_ids = (fixture == 0   ? 0ULL
                            : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                           : 19ULL);
    s.engine_shader_resources = (fixture == 0   ? 0ULL
                                 : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                                : 20ULL);
    s.retired_shader_resources = (fixture == 0   ? 0ULL
                                  : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                                 : 21ULL);
    s.shader_reuses = (fixture == 0 ? 0ULL : fixture == 1 ? std::numeric_limits<unsigned long long>::max() : 22ULL);
    s.volume_ray_marching = (fixture % 2 != 0);
    s.mesh_variant_resources = (fixture == 0   ? 0ULL
                                : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                               : 24ULL);
    s.mesh_variant_bytes = (fixture == 0   ? 0ULL
                            : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                           : 25ULL);
    s.retained_mesh_source_bytes = (fixture == 0   ? 0ULL
                                    : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                                   : 26ULL);
    s.displacement_rebuilds = (fixture == 0   ? 0ULL
                               : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                              : 27ULL);
    s.subdivision_control_faces = (fixture == 0   ? 0ULL
                                   : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                                  : 28ULL);
    s.subdivision_vertices = (fixture == 0   ? 0ULL
                              : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                             : 29ULL);
    s.subdivision_triangles = (fixture == 0   ? 0ULL
                               : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                              : 30ULL);
    s.subdivision_tessellations = (fixture == 0   ? 0ULL
                                   : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                                  : 31ULL);
    s.image_iris_preparations = (fixture == 0   ? 0ULL
                                 : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                                : 32ULL);
    s.image_iris_table_bytes = (fixture == 0   ? 0ULL
                                : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                               : 33ULL);
    s.mesh_family_resources = (fixture == 0   ? 0ULL
                               : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                              : 34ULL);
    s.mesh_family_members = (fixture == 0   ? 0ULL
                             : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                            : 35ULL);
    s.mesh_variant_bindings = (fixture == 0   ? 0ULL
                               : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                              : 36ULL);
    s.mesh_refits = fixture == 0 ? 0ULL : fixture == 1 ? std::numeric_limits<unsigned long long>::max() : 43ULL;
    s.mesh_metric_records = (fixture == 0   ? 0ULL
                             : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                            : 37ULL);
    s.dielectric_priority_objects = (fixture == 0   ? 0ULL
                                     : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                                    : 38ULL);
    s.nested_probe_unresolved = (fixture == 0   ? 0ULL
                                 : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                                : 39ULL);
    s.color_epoch = (fixture == 0 ? 0ULL : fixture == 1 ? std::numeric_limits<unsigned long long>::max() : 40ULL);
    s.color_target_epoch = (fixture == 0   ? 0ULL
                            : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                           : 41ULL);
    s.unchanged_reuses = (fixture == 0 ? 0ULL : fixture == 1 ? std::numeric_limits<unsigned long long>::max() : 42ULL);
    s.volume_vector_merges = (fixture == 0   ? 0ULL
                              : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                             : 43ULL);
    s.volume_vector_fields = (fixture == 0   ? 0ULL
                              : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                             : 44ULL);
    s.volume_vector_source_bytes = (fixture == 0   ? 0ULL
                                    : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                                   : 45ULL);
    s.image_iris_preparation_seconds = (fixture == 0 ? 0.0 : 46.125);
    s.engine_nested_dielectrics = (fixture % 2 != 0);
    s.nested_stack_depth_clamped = (fixture % 2 != 0);
    s.color_update_pending = (fixture % 2 != 0);
    s.dielectric_priority_min = (fixture == 0 ? 0 : -50);
    s.dielectric_priority_max = (fixture == 0 ? 0 : -51);
    s.nested_stack_depth = (fixture == 0 ? 0 : -52);
    s.current_sample = (fixture == 0 ? 0 : -53);
    a.material_motion_cooks = (fixture == 0   ? 0ULL
                               : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                              : 54ULL);
    a.material_motion_samples = (fixture == 0   ? 0ULL
                                 : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                                : 55ULL);
    a.material_motion_cache_hits = (fixture == 0   ? 0ULL
                                    : fixture == 1 ? std::numeric_limits<unsigned long long>::max()
                                                   : 56ULL);
    s.state = static_cast<ch::SessionState>(fixture % 8);
    if (fixture) {
      s.message = "message \u00e9";
      s.error = "error";
      s.denoiser = "oidn";
      s.denoise_device_id = "CPU";
      s.render_device_ids = {"CPU", "OPTIX:0"};
      a.motion_warnings = {"warning one", "warning two"};
      s.direct_attribute_updates = 57;
      s.expanded_attribute_updates = 58;
      s.attribute_fallback_count = 59;
      s.attribute_fallback_reason = "fallback \u00e9";
      s.source_diagnostics = {{"test.code", "/obj/source", "mesh", "issue", "continue", "warning", ""},
                              {"test.info", "", "", "info", "", "info", "density"}};
    }
    if (fixture == 10)
      s.message = std::string("\xff", 1);
    if (fixture == 11)
      s.denoiser = std::string("\xff", 1);
    PyObject *value = nullptr;
    try {
      value = old ? legacy::status_value(s, a) : ch::python_status_value(s, a).release().ptr();
    } catch (pybind11::error_already_set &error) {
      error.restore();
    } catch (const std::exception &e) {
      legacy::failure(e);
    }
    if (!value) {
      PyObject *type = nullptr, *error = nullptr, *trace = nullptr;
      PyErr_Fetch(&type, &error, &trace);
      if (!type)
        return 2;
      PyErr_NormalizeException(&type, &error, &trace);
      PyObject *name = PyObject_GetAttrString(type, "__name__");
      PyObject *message = PyObject_Str(error);
      value = Py_BuildValue("{s:O,s:O}", "exception", name, "message", message);
      Py_XDECREF(name);
      Py_XDECREF(message);
      Py_XDECREF(type);
      Py_XDECREF(error);
      Py_XDECREF(trace);
    }
    PyDict_SetItemString(globals, "value", value);
    Py_DECREF(value);
    if (PyRun_SimpleString("rows.append(typed(value))") < 0)
      return 3;
    ch::test_caps = ch::Capabilities{};
    ch::test_caps.engine_commit = fixture ? "engine-\u00e9" : "";
    ch::test_caps.image_iris = fixture % 2;
    ch::test_caps.gaussian_splats = fixture % 2;
    ch::test_caps.osl = fixture % 2;
    ch::test_caps.bdpt_prototype = fixture % 2;
    ch::test_caps.path_guiding = fixture % 2;
    ch::test_caps.volumes = fixture % 2;
    ch::test_caps.meshes = fixture % 2;
    value = old ? legacy::capabilities(nullptr, nullptr) : ch::python_capabilities_value(ch::test_caps).release().ptr();
    if (!value) {
      PyErr_Print();
      return 4;
    }
    PyDict_SetItemString(globals, "value", value);
    Py_DECREF(value);
    if (PyRun_SimpleString("rows.append(typed(value))") < 0)
      return 5;
  }
  // Repeated polling returns independent dictionaries/lists and releases refs.
  for (int i = 0; i < 2000; ++i) {
    auto *value = old ? legacy::status_value({}) : ch::python_status_value({}).release().ptr();
    if (!value || PyErr_Occurred())
      return 6;
    Py_DECREF(value);
  }
  if (PyRun_SimpleString("print(json.dumps(rows,sort_keys=True,ensure_ascii=True))") < 0)
    return 7;
  Py_Finalize();
  return 0;
}
