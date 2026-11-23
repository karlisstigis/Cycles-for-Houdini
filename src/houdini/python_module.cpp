// SPDX-License-Identifier: Apache-2.0
#include "python_binding_detail.h"
#include "python_bindings.h"
#include "tile_display.h"
#include "packed_blender_bounds.h"
#include "blend_mesh.h"
#include "deformation_adapter.h"
#include "../core/color_internal.h"
#include <PXL/PXL_OCIO.h>
#include <UT/UT_StringHolder.h>
#include <pybind11/stl.h>

using namespace cycles_houdini::python_detail;

PYBIND11_MODULE(_cycles_houdini, m) {
  m.doc() = "Native Cycles Houdini development integration";
  ch::set_color_configuration_path_resolver([]() {
    const auto path = PXL_OCIO::configFilePath();
    if (!path.isstring())
      throw std::invalid_argument("Houdini has no resolved OCIO configuration file");
    return std::string(path.c_str());
  });
  m.def("_shutdown_native_displays", &ch::TileDisplay::shutdown_all);
  // Registered during module initialization, before the IPR registry's own
  // cleanup hook. Python atexit runs the registry cleanup first (LIFO), then
  // drains native display threads while this extension remains loaded.
  py::module_::import("atexit").attr("register")(m.attr("_shutdown_native_displays"));
  ch::bind_python_status(m, read_status);
  // The project catch-all in bind_python_status maps std::exception to
  // RuntimeError. Register these afterward so IPR sees their distinct types.
  py::register_local_exception<ch::PreviousColorConfiguration>(m, "PreviousColorConfiguration", PyExc_RuntimeError);
  py::register_local_exception<ch::DisplayConnectionError>(m, "DisplayConnectionError", PyExc_RuntimeError);
  py::register_local_exception<ch::DisplayRemoteQuit>(m, "DisplayRemoteQuit", PyExc_RuntimeError);
  py::register_local_exception<ch::MotionCaptureInterrupted>(m, "MotionCaptureInterrupted", PyExc_RuntimeError);
  py::class_<FramePixels>(m, "FramePixels", py::buffer_protocol(), py::module_local())
      .def_buffer([](FramePixels &pixels) {
        return py::buffer_info(const_cast<float *>(pixels.data), 1, py::format_descriptor<unsigned char>::format(), 1,
                               {pixels.size}, {1}, true);
      });
  m.def("blend_mesh", &blend_mesh, py::arg("record"), py::arg("arrays"), py::arg("palette"),
        py::arg("previous") = py::none(), py::arg("reuse_layout") = py::bool_(false), py::pos_only());
  m.def("blend_curves", &blend_curves, py::arg("record"), py::arg("arrays"), py::arg("palette"), py::pos_only());
  m.def("blend_points", &blend_points, py::arg("record"), py::arg("arrays"), py::arg("palette"), py::pos_only());
  m.def("blend_mesh_nbytes", &blend_mesh_nbytes, py::arg("capsule"), py::pos_only());
  m.def("blend_mesh_storage", &blend_mesh_storage, py::arg("capsule"), py::pos_only());
  m.def(
      "blend_mesh_bounds",
      [](py::handle capsule) { return ch::blend_mesh_bounds(*ch::blend_mesh_from_capsule(capsule)); },
      py::arg("capsule"), py::pos_only());
  m.def("scene_definition_fallback", &scene_definition_fallback, py::arg("path"), py::arg("script"), py::arg("inputs"),
        py::arg("outputs"), py::pos_only());
  m.def("clear_scene_definitions", &clear_scene_definitions);
  m.def("resolve_camera_motion", &resolve_camera_motion, py::arg("nominal"), py::arg("sample_values"),
        py::arg("shutter"), py::arg("source"), py::arg("vectors") = 0, py::arg("width") = 1, py::arg("height") = 1,
        py::arg("pixel_aspect") = 1, py::pos_only());
  m.def("prepare_navigation", &prepare_navigation, py::arg("capsule"), py::arg("baseline"), py::arg("data"),
        py::arg("time"), py::arg("previous") = omitted(), py::pos_only());
  m.def("navigation_snapshot", &navigation_snapshot, py::arg("capsule"), py::arg("navigation"),
        py::arg("keep_unchanged") = 0, py::arg("camera_only") = 0, py::pos_only());
  m.def("navigation_stats", &navigation_stats);
  m.def("release_navigation_memo", &release_navigation_memo);
  m.def("prepare_object_navigation", &prepare_object_navigation, py::arg("capture"), py::arg("baseline"),
        py::arg("data"), py::arg("time"), py::arg("previous") = omitted(), py::pos_only());
  m.def("navigation_info", &navigation_info, py::arg("capsule"), py::pos_only());
  m.def("navigation_basis_equal", &navigation_basis_equal, py::arg("before"), py::arg("after"), py::pos_only());
  m.def("navigation_context_equal", &navigation_context_equal, py::arg("capsule"), py::arg("navigation"),
        py::pos_only());
  m.def("preserve_geometry", &preserve_geometry, py::arg("path"), py::arg("time"), py::arg("copy") = 0,
        py::arg("material") = omitted(), py::pos_only());
  m.def("convert_preserved_mesh", &convert_preserved_mesh, py::arg("capsule"), py::pos_only());
  m.def("convert_preserved_curves", &convert_preserved_curves, py::arg("capsule"), py::arg("divisions") = 16,
        py::arg("velocity") = 0, py::arg("names") = omitted(), py::pos_only());
  m.def("preserve_deformation", &preserve_deformation, py::arg("path"), py::arg("time"), py::arg("fps"),
        py::arg("shutter"), py::arg("samples") = 3, py::arg("material") = omitted(), py::pos_only());
  m.def("convert_preserved_deformation", &convert_preserved_deformation, py::arg("capsule"), py::pos_only());
  m.def("probe_gridless_volume", &probe_gridless_volume);
  m.def("preserve_volume", &preserve_volume, py::arg("path"), py::arg("time"), py::pos_only());
  m.def("convert_preserved_volume", &convert_preserved_volume, py::arg("capsule"), py::pos_only());
  m.def("write_dense_volume_fixture", &write_dense_volume_fixture, py::arg("path"), py::arg("mode") = 0,
        py::pos_only());
  m.def("preserve_packed_geometry", &preserve_packed_geometry, py::arg("path"), py::arg("time"),
        py::arg("material") = omitted(), py::pos_only());
  m.def("packed_blender_references", &packed_blender_references, py::arg("path"), py::arg("time"), py::pos_only());
  m.def("publish_blender_reference_bounds", &ch::publish_blender_reference_bounds, py::arg("reference"),
        py::arg("frame"), py::arg("record"), py::pos_only());
  m.def("serialized_blender_reference_bounds", &ch::serialized_blender_reference_bounds, py::arg("reference"),
        py::arg("frame"), py::pos_only());
  m.def("clear_blender_reference_bounds", &ch::clear_blender_reference_bounds);
  m.def("blender_reference_preview_valid", &ch::blender_reference_preview_valid);
  m.def("invalidate_blender_reference_preview", &ch::invalidate_blender_reference_preview);
  m.def("restore_blender_reference_preview", &ch::restore_blender_reference_preview);
  m.def("convert_preserved_packed_geometry", &convert_preserved_packed_geometry, py::arg("capsule"), py::pos_only());
  m.def("preserve_native_scene", &preserve_native_scene, py::arg("data"), py::arg("time"), py::arg("baseline"),
        py::pos_only());
  m.def("convert_preserved_native_scene", &convert_preserved_native_scene, py::arg("capsule"), py::pos_only());
  m.def("preserve_material_graphs", &preserve_material_graphs, py::arg("paths"), py::arg("resolver"), py::arg("time"),
        py::pos_only());
  m.def("convert_preserved_material_graphs", &convert_preserved_material_graphs, py::arg("capsule"), py::arg("paths"),
        py::pos_only());
  m.def("pass_catalogue", &pass_catalogue);
  m.def("set_preview_pass", &set_preview_pass, py::arg("capsule"), py::arg("pass"), py::arg("focus") = 0,
        py::pos_only());
  m.def("request_thumbnail", &request_thumbnail, py::arg("node"), py::arg("size") = 128, py::pos_only());
  m.def("take_thumbnails", &take_thumbnails);
  m.def("current_thumbnail", &current_thumbnail, py::arg("token"), py::pos_only());
  m.def("publish_thumbnail", &publish_thumbnail, py::arg("token"), py::arg("pixels"), py::arg("error") = "",
        py::pos_only());
  m.def("thumbnail_stats", &thumbnail_stats);
  m.def("thumbnail_pixels", &thumbnail_pixels, py::arg("node"), py::pos_only());
  m.def("thumbnail_fixture", &thumbnail_fixture, py::arg("capsule"), py::arg("domain") = "surface",
        py::arg("world_medium") = py::bool_(false), py::pos_only());
  m.def("encode_preview", &encode_preview, py::arg("pass"), py::arg("focus") = 0, py::pos_only());
  m.def("focus_frame", &focus_frame, py::arg("capsule"), py::pos_only());
  m.def("sample_focus", &sample_focus, py::arg("capsule"), py::arg("x"), py::arg("y"), py::pos_only());
  m.def("engine_create", &engine_create, py::arg("capture"), py::pos_only());
  m.def("apply_snapshot", &apply_snapshot, py::arg("capsule"), py::arg("desired"), py::arg("ack") = omitted(),
        py::pos_only());
  m.def("owned_frame", &owned_frame, py::arg("capsule"), py::arg("after") = 0, py::pos_only());
  m.def("present_frame", &present_frame, py::arg("capsule"), py::arg("image"), py::arg("sequence") = 0, py::pos_only());
  m.def("_test_wait_display", &test_wait_display, py::arg("capsule"), py::pos_only());
  m.def("encode_config", &encode_config, py::arg("capture"), py::arg("desired"), py::arg("root"), py::pos_only());
  bind_wire_transport(m);
  m.def("encode_snapshot", &encode_snapshot, py::arg("desired"), py::arg("ack") = omitted(),
        py::arg("reuse_mesh_payloads") = 1, py::arg("root_poses") = 1, py::pos_only());
  m.def("decode_reply", &decode_reply, py::arg("packet"), py::arg("desired"), py::pos_only());
  m.def("frame_receiver", &frame_receiver);
  m.def("decode_render_reply", &decode_render_reply, py::arg("packet"), py::arg("receiver"), py::pos_only());
  m.def("encode_render_reply", &encode_render_reply, py::arg("capsule"), py::arg("after") = 0, py::pos_only());
  m.def("worker_startup_snapshot", &worker_startup_snapshot);
  m.def("encode_color_configuration", &encode_color_configuration);
  m.def("snapshot_color_identity", &snapshot_color_identity, py::arg("capsule"), py::pos_only());
  m.def("snapshot_info", &snapshot_info, py::arg("capsule"), py::arg("ack") = omitted(), py::pos_only());
  m.def("snapshot_digest", &snapshot_digest, py::arg("capsule"), py::pos_only());
  m.def("native_texture_color_info", &texture_color_info, py::arg("filename"), py::arg("read_as_is") = 1,
        py::arg("observe_texture_cache") = 0, py::pos_only());
  m.def("texture_cache_plan", &texture_cache_plan, py::arg("snapshot"), py::pos_only());
  m.def("texture_cache_generate", &texture_cache_generate, py::arg("item"), py::pos_only());
  m.def("freeze_cop_image", &freeze_cop_image, py::arg("source"), py::arg("time"), py::pos_only());
  m.def("cop_image_info", &cop_image_info, py::arg("value"), py::pos_only());
  m.def("acquire_images", &acquire_images, py::arg("capsule"), py::pos_only());
  m.def("convert_images", &convert_images, py::arg("capsule"), py::pos_only());
  m.def("cop_capture_stats", &cop_capture_stats, py::arg("capsule"), py::pos_only());
  m.def("cop_image_sources", &cop_image_sources, py::arg("capsule"), py::pos_only());
  m.def("cop_image_pixels", &cop_image_pixels, py::arg("value"), py::pos_only());
  m.def("cop_image_source_info", &cop_image_source_info, py::arg("source"), py::pos_only());
  m.def("process_memory_json", &process_memory_json);
  m.def("shader_catalogue_json", &shader_catalogue_json);
  m.def("shader_curve_table", &shader_curve_table, py::arg("source"), py::arg("kind"), py::pos_only());
  m.def("ramp_preview", &ramp_preview, py::arg("color"), py::arg("alpha"), py::arg("factors"), py::arg("options"),
        py::pos_only());
  m.def("ramp_preview_sample", &ramp_preview_sample, py::arg("capsule"), py::arg("factor"), py::pos_only());
  m.def("vop_output_override", &vop_output_override, py::arg("id"), py::arg("output"), py::pos_only());
  m.def("vop_input_activity", &vop_input_activity, py::arg("id"), py::arg("time"), py::arg("values") = omitted(),
        py::pos_only());
  m.def("vop_input_controls", &vop_input_controls, py::arg("id"), py::pos_only());
  m.def("vop_connector_layout", &vop_connector_layout, py::arg("id"), py::pos_only());
  m.def("textures_changed", &textures_changed, py::arg("capsule"), py::pos_only());
  m.def("texture_inputs", &texture_inputs, py::arg("capsule"), py::pos_only());
  m.def("point_instance_table", &point_instance_table, py::arg("data"), py::arg("time"), py::pos_only());
  m.def("initialize", &initialize, py::arg("path"), py::arg("cache") = "", py::pos_only());
  m.def("devices", &devices);
  m.def("create", &create, py::arg("device"), py::arg("width"), py::arg("height"), py::arg("samples"),
        py::arg("interactive") = py::bool_(true), py::arg("diagnostics") = py::bool_(false),
        py::arg("texture_cache") = py::bool_(true), py::arg("auto_texture_cache") = py::bool_(false),
        py::arg("denoising") = py::none(), py::arg("capture_only") = py::bool_(false), py::arg("threads") = 0,
        py::arg("passes") = py::none(), py::arg("progressive_display") = py::bool_(false),
        py::arg("use_tiling") = py::bool_(false), py::arg("tile_size") = 2048, py::arg("hair_subdivisions") = 2,
        py::arg("refit_meshes") = py::bool_(false), py::pos_only());
  m.def("fixture", &fixture, py::arg("capsule"), py::pos_only());
  m.def("apply", &apply, py::arg("capsule"), py::arg("data"), py::arg("time"), py::arg("reuse") = 0, py::pos_only());
  m.def("acquire_apply", &acquire_apply, py::arg("capsule"), py::arg("data"), py::arg("time"), py::pos_only());
  m.def("prewarm_apply", &prewarm_apply, py::arg("capsule"), py::pos_only());
  m.def("expand_apply", &expand_apply, py::arg("capsule"), py::pos_only());
  m.def("prepare_apply", &prepare_apply, py::arg("capsule"), py::arg("data"), py::arg("time"), py::arg("reuse") = 0,
        py::arg("warm") = omitted(), py::pos_only());
  m.def("convert_apply", &convert_apply, py::arg("capsule"), py::pos_only());
  m.def("prepare_material_edit", &prepare_material_edit, py::arg("capsule"), py::arg("resolver"), py::arg("paths"),
        py::arg("time"), py::pos_only());
  m.def("finish_apply", &finish_apply, py::arg("capsule"), py::pos_only());
  m.def("discard_apply", &discard_apply, py::arg("capsule"), py::pos_only());
  m.def("retire_capture", &retire_capture, py::arg("capsule"), py::pos_only());
  m.def("_test_retire_capture_rendezvous", &test_retire_capture_rendezvous, py::arg("capsule"), py::arg("entered"),
        py::arg("release"), py::pos_only());
  m.def("frame", &frame, py::arg("capsule"), py::arg("after") = 0, py::pos_only());
  m.def("frame_info", &frame_info, py::arg("capsule"), py::arg("after") = 0, py::pos_only());
  m.def("output_metadata", &output_metadata, py::arg("capsule"), py::pos_only());
  m.def("diagnostics", &diagnostics, py::arg("capsule"), py::pos_only());
  m.def("native_object_keys", &native_object_keys, py::arg("capsule"), py::pos_only());
  m.def("native_material_references", &native_material_references, py::arg("capsule"), py::pos_only());
  m.def("native_material_paths", &native_material_paths, py::arg("capsule"), py::pos_only());
  m.def("native_stylesheet_resources", &native_stylesheet_resources, py::arg("capsule"), py::pos_only());
  m.def("stylesheet_source_signature", &stylesheet_source_signature, py::arg("input"), py::pos_only());
  m.def("object_stylesheet", &object_stylesheet, py::arg("path"), py::arg("input"), py::arg("own"), py::arg("tags"),
        py::arg("time") = omitted(), py::pos_only());
  m.def("native_stats", &native_stats, py::arg("capsule"), py::pos_only());
  m.def("pause", &pause, py::arg("capsule"), py::arg("paused"), py::pos_only());
  m.def("cancel", &cancel, py::arg("capsule"), py::pos_only());
  m.def("close", &close_session, py::arg("capsule"), py::pos_only());
  m.def("encode_output", &encode_output, py::arg("path"), py::arg("options") = omitted(), py::pos_only());
  m.def("write_exr", &write_exr, py::arg("capsule"), py::arg("path"), py::arg("options") = omitted(), py::pos_only());
  m.def("inspect_existing_exr", &inspect_existing_exr, py::arg("path"), py::arg("expected_parts"), py::arg("validate"),
        py::arg("checkpoint") = py::none(), py::pos_only());
  m.def("display_gpu_input", &display_gpu_input, py::arg("capsule"), py::arg("data"), py::arg("known_key") = "",
        py::arg("copy_pixels") = 1, py::pos_only());
  m.def("expand_display_rgba", &expand_display_rgba, py::arg("input"), py::arg("source_width"),
        py::arg("source_height"), py::arg("width"), py::arg("height"), py::pos_only());
  m.def("compact_frame_fixture", &compact_frame_fixture, py::arg("full_coverage") = false, py::pos_only());
  m.def("display_frame_fixture", &display_frame_fixture, py::arg("pixels"), py::arg("width"), py::arg("height"),
        py::pos_only());
  m.def("blackbody_temperature_color", &blackbody_temperature_color, py::arg("kelvin"), py::pos_only());
  m.def("color_policy", &color_policy);
  m.def("synchronize_color_configuration", &synchronize_color);
  m.def("display_image", &display_image, py::arg("capsule"), py::arg("data"), py::pos_only());
  m.def("open_display", &open_display, py::arg("capsule"), py::arg("destination"), py::arg("socket") = "",
        py::arg("options") = std::map<std::string, std::string>{}, py::pos_only());
  m.def("display_ready", &display_ready, py::arg("capsule"), py::pos_only());
  m.def("set_native_display_status", &set_native_display_status, py::arg("capsule"), py::arg("message"),
        py::pos_only());
  m.def("present", &present, py::arg("capsule"), py::pos_only());
  m.def("_test_present_frame_rendezvous", &test_present_frame_rendezvous, py::arg("capsule"), py::arg("image"),
        py::arg("entered"), py::arg("release"), py::pos_only());
  m.def("close_display", &close_display, py::arg("capsule"), py::pos_only());
  m.def("geometry_lease_count", [] { return geometry_leases.load(); });
  m.def("validate_aovs", [](py::handle value) { aov_definitions(value); }, py::arg("value"), py::pos_only());
  m.def(
      "validate_lightgroups", [](py::handle value) { lightgroup_definitions(value); }, py::arg("value"),
      py::pos_only());
  m.def(
      "validate_pass_outputs",
      [](py::handle aovs, py::handle groups) {
        ch::validate_pass_outputs(aov_definitions(aovs), lightgroup_definitions(groups));
      },
      py::arg("aovs"), py::arg("groups"), py::pos_only());
  m.def(
      "validate_update_metadata",
      [](py::handle object) {
        auto value = object;
        if (auto list = item(value, "materials", false))
          for (auto material : sequence(list))
            if (auto index = item(material, "pass_index", false))
              pass_index(index);
        if (auto list = item(value, "geometry", false))
          for (auto request : sequence(list))
            if (auto properties = item(request, "object_properties", false)) {
              if (auto index = item(properties, "pass_index", false))
                pass_index(index);
              if (auto priority = item(properties, "dielectric_priority", false))
                dielectric_priority(priority);
            }
        if (auto passes = item(value, "passes", false))
          read_pass_names(passes);
        auto aovs = item(value, "aovs", false);
        auto groups = item(value, "lightgroups", false);
        if (aovs && groups)
          ch::validate_pass_outputs(aov_definitions(aovs), lightgroup_definitions(groups));
        else if (aovs)
          aov_definitions(aovs);
        else if (groups)
          lightgroup_definitions(groups);
        if (auto crypto = item(value, "cryptomatte", false))
          cryptomatte_settings(crypto);
      },
      py::arg("value"), py::pos_only());
}
