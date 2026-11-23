// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <pybind11/pybind11.h>
#include "cycles_houdini/session.h"
#include <array>
#include <atomic>
#include <cstring>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace cycles_houdini {
struct ExtractionStats;
struct GeometryRequest;
struct MaterialPreviewRequest;
struct NativeMaterialReference;
struct NativeMotionSettings;
struct NativeMotionWindow;
struct OwnedSceneSnapshot;
} // namespace cycles_houdini

namespace cycles_houdini::python_detail {
namespace ch = cycles_houdini;
namespace py = pybind11;

struct Handle;
ch::ExtractionStats capture_stats(const Handle &h);

// All native work scopes release/reacquire through pybind11 RAII.
using ReleaseGIL = py::gil_scoped_release;

// Python handles remain typed and owning values stay in pybind11 objects.
// The two narrow CPython storage operations below document their contracts.
// pybind11 exposes immutable bytes. Large output packets are allocated once,
// filled before publication while privately owned, then exposed as bytes.
// This is the sole writable-bytes CPython interop; never pass a published value.
char *unpublished_bytes_data(py::handle value);

const char *c_string_checked(const std::string &value);

py::capsule omitted();

bool is_omitted(py::handle value);

template <class T> T integer(py::handle value, const char *message) {
  // A nonconverting pybind11 arithmetic caster accepts __index__, as the
  // former PyLong_As* calls did, without widening the contract to __int__.
  const auto owner = py::reinterpret_borrow<py::object>(value);
  py::detail::make_caster<T> caster;
  if (!caster.load(owner, false))
    throw std::invalid_argument(message);
  return py::detail::cast_op<T>(std::move(caster));
}

// Explicit pybind11 scalar argument conversion preserves the former argument
// parser's TypeError/OverflowError boundary before native validation begins.
template <class T> T argument_integer(py::handle value) {
  auto indexed = py::module_::import("operator").attr("index")(value);
  py::detail::make_caster<T> caster;
  if (!caster.load(indexed, false)) {
    py::set_error(PyExc_OverflowError, "Python int too large to convert to a native integer");
    throw py::error_already_set();
  }
  return py::detail::cast_op<T>(std::move(caster));
}

template <class T> T argument_real(py::handle value) {
  if (!py::hasattr(value, "__float__") && !py::hasattr(value, "__index__"))
    throw py::type_error("A real number is required");
  return static_cast<T>(py::float_(py::reinterpret_borrow<py::object>(value)).cast<double>());
}

bool truth(py::handle value);

struct ReadBuffer {
  py::buffer_info value;
  void request(py::handle value_object) {
    if (!py::isinstance<py::buffer>(value_object))
      throw py::type_error("Expected a buffer");
    value = py::reinterpret_borrow<py::buffer>(value_object).request();
    // Match PyBUF_CONTIG_RO: a flat C-contiguous byte span is required.
    if (!value.size)
      return; // Empty exports are contiguous regardless of strides.
    py::ssize_t stride = value.itemsize;
    for (py::ssize_t i = value.ndim; i-- > 0;) {
      if (value.shape[i] > 1 && value.strides[i] != stride)
        throw py::buffer_error("Expected a C-contiguous buffer");
      stride *= value.shape[i];
    }
  }
};

template <class T>
T *capsule_pointer(py::handle value, const char *name, const char *message = "Invalid native capsule") {
  if (!value || !py::isinstance<py::capsule>(value))
    throw std::invalid_argument(message);
  const auto capsule = py::reinterpret_borrow<py::capsule>(value);
  if (!capsule.name() || std::strcmp(capsule.name(), name) != 0)
    throw std::invalid_argument(message);
  auto *pointer = static_cast<T *>(capsule.get_pointer());
  if (!pointer)
    throw std::invalid_argument(message);
  return pointer;
}

Handle &handle(py::handle obj);

template <class T> void destroy_owned(void *value) {
  ReleaseGIL release;
  delete static_cast<T *>(value);
}

using SnapshotPtr = std::shared_ptr<const ch::OwnedSceneSnapshot>;

using FramePtr = std::shared_ptr<const ch::FrameEvent>;

SnapshotPtr snapshot(py::handle value);
// Protocol 91 root-pose snapshot -> one whose object table holds every instance pose.
SnapshotPtr materialize_root_poses(const SnapshotPtr &value);

py::object frame_capsule(FramePtr image);

FramePtr read_image(py::handle value, std::uint64_t after = 0);

// The pybind11 buffer exporter owns an immutable frame. Each memoryview
// retains the exporter, keeping zero-copy storage alive beyond session close.
struct FramePixels {
  FramePtr owner;
  const float *data{};
  py::ssize_t size{};
  ~FramePixels() {
    ReleaseGIL release;
    owner.reset();
  }
};

py::memoryview frame_pixels_view(const FramePtr &frame, const ch::ImagePlane &plane);

py::object item(py::handle dictionary, const char *key, bool required = true);

std::string string(py::handle object);

double number(py::handle object);

ch::DenoiseSettings denoise_settings(py::handle value);

ch::CryptomatteSettings cryptomatte_settings(py::handle value);

ch::RenderSettings render_settings(py::handle value, int samples);

py::dict rendering_value(const ch::RenderSettings &s);

void denoise_metadata(py::dict result, const std::string &backend, const std::string &device);

// Snapshot owning entries before callbacks can mutate their original list.
std::vector<py::object> sequence(py::handle object);

ch::Float3 vector3(py::handle object);

ch::Matrix matrix(py::handle object);

std::string shader_string(py::handle object);

std::vector<ch::AovDefinition> aov_definitions(py::handle value);

std::vector<std::string> lightgroup_definitions(py::handle value);

ch::ShaderEndpoint shader_endpoint(py::handle value);

ch::ShaderValue shader_value(py::handle value, bool integer_array = false, bool ramp_controls = false,
                             int control_limit = 168);

ch::ShaderGraphData shader_graph(py::handle value);

int pass_index(py::handle value);

int dielectric_priority(py::handle value);

int world_integer(py::handle value);

std::vector<ch::SourceDiagnostic> source_issues(py::handle values);

ch::MaterialData material(py::handle value);

py::list material_reference_list(const std::vector<ch::NativeMaterialReference> &references);

std::string python_error_message(const py::error_already_set &error);

ch::MaterialData prepare_material_graph(py::handle resolver, const ch::NativeMaterialReference &reference, double time);
py::object prepare_material_edit(py::handle capsule_arg, py::handle resolver_arg, py::handle paths_arg,
                                 py::handle time_arg);

void initialize(py::str path_arg, py::str cache_arg);

py::list devices();

py::capsule create(py::handle device, py::handle width_arg, py::handle height_arg, py::handle samples_arg,
                   py::handle interactive, py::handle diagnostics, py::handle texture_cache,
                   py::handle auto_texture_cache, py::handle denoising, py::handle capture_only, py::handle threads_arg,
                   py::handle passes, py::handle progressive_display, py::handle use_tiling, py::handle tile_size,
                   py::handle hair_subdivisions, py::handle refit_meshes);

py::object fixture(py::handle capsule);

ch::MaterialPreviewRequest thumbnail_request(py::handle object);

py::tuple thumbnail_tuple(const ch::MaterialPreviewRequest &value);

py::tuple request_thumbnail(py::handle node_arg, py::handle size_arg);

py::list take_thumbnails();

bool current_thumbnail(py::handle token);

bool publish_thumbnail(py::handle token, py::handle pixels, py::str error_arg);

py::dict thumbnail_stats();

py::bytes thumbnail_pixels(py::handle node_arg);

py::capsule thumbnail_fixture(py::handle capsule, py::str domain_arg, py::handle world_medium);

ch::GeometryRequest geometry_request(py::handle value, int depth = 0);

py::object blend_mesh(py::handle record, py::handle arrays, py::handle palette, py::handle previous,
                      py::handle reuse_layout);

py::object blend_curves(py::handle record, py::handle arrays, py::handle palette);

py::object blend_points(py::handle record, py::handle arrays, py::handle palette);

std::size_t blend_mesh_nbytes(py::handle capsule);

py::dict blend_mesh_storage(py::handle capsule);

unsigned long long unsigned_argument_mask(py::handle value, int position = 2);

std::string argument_script_buffer(py::handle value);

std::vector<ch::GeometryRequest> geometry_requests(py::handle list);

ch::GeometryRequest geometry_request(py::handle value, int depth);

ch::NativeMotionSettings motion_settings(py::handle motion_data);

// A capture transaction retains only native values after host acquisition.
// Its capsule retains the handle; exclusive adapter ownership lasts until finish
// or discard. No Python callback is reachable by the conversion thread.
ch::PanoramaSettings camera_panorama(py::handle value);

ch::CameraSampling camera_sampling(py::handle value);

void prepare_camera_update(ch::SceneUpdate &update, py::handle data, const ch::NativeMotionWindow &window, int width,
                           int height);

std::vector<std::string> read_pass_names(py::handle value);

void destroy_apply_job(void *pointer);

py::object acquire_apply(py::handle capsule_arg, py::handle data_arg, py::handle time_arg);

py::object prewarm_apply(py::handle capsule_arg);

py::object prepare_apply(py::handle capsule_arg, py::handle data_arg, py::handle time_arg, py::handle reuse_arg,
                         py::handle warm_arg);

py::object expand_apply(py::handle capsule_arg);

py::object convert_apply(py::handle capsule_arg);

py::object acquire_images(py::handle capsule_arg);

py::object convert_images(py::handle capsule_arg);

py::object finish_apply(py::handle capsule_arg);

py::object discard_apply(py::handle capsule_arg);

py::object apply(py::handle capsule_arg, py::handle data_arg, py::handle time_arg, py::handle reuse_arg);

void motion_warning_metadata(py::dict result, const ch::ExtractionStats &stats);

void source_diagnostic_metadata(py::dict result, const std::vector<ch::SourceDiagnostic> &issues);

py::dict status_value(const ch::SessionStatus &status, const ch::ExtractionStats &stats);

void point_instance_status(py::dict result, std::array<std::uint64_t, 3> counts, double seconds);

py::dict read_status(py::handle capsule);

py::bytes plane_pixels(const ch::FrameEvent &image, const ch::ImagePlane &plane);

py::object frame(py::handle capsule_arg, py::handle after_arg);

py::dict pass_value(const ch::PassDescriptor &pass);

py::object frame_info(py::handle capsule_arg, py::handle after_arg);

py::dict diagnostics_value(const ch::SessionDiagnostics &data);

py::object diagnostics(py::handle capsule_arg);

py::object native_object_keys(py::handle capsule_arg);

py::object native_material_references(py::handle capsule_arg);

py::object native_material_paths(py::handle capsule_arg);

py::list string_list(const std::vector<std::string> &values);

py::object native_stylesheet_resources(py::handle capsule_arg);

py::object stylesheet_source_signature(py::handle input_arg);

py::object object_stylesheet(py::str path_arg, py::handle input_arg, py::str own_arg, py::str tags_arg,
                             py::handle time_arg);

py::object native_stats(py::handle capsule_arg);

py::object scene_definition_fallback(py::str path_arg, py::handle script_arg, py::handle inputs_arg,
                                     py::handle outputs_arg);

py::object clear_scene_definitions();

py::object cancel(py::handle capsule_arg);

py::object retire_capture(py::handle capsule_arg);

py::object test_retire_capture_rendezvous(py::handle capsule_arg, py::handle entered_arg, py::handle release_arg);

py::object close_session(py::handle capsule_arg);

py::object pause(py::handle capsule_arg, py::handle paused_arg);

ch::RenderSession::OutputOptions output_options(py::handle data);

py::object write_exr(py::handle capsule_arg, py::str path_arg, py::handle options_arg);
void inspect_existing_exr(const std::string &filename, int expected_parts, py::function validate,
                          py::object checkpoint);

py::object encode_output(py::str path_arg, py::handle options_arg);

py::object encode_preview(py::str pass_arg, py::handle focus_arg);

py::object color_policy();

py::object blackbody_temperature_color(py::handle kelvin_arg);

py::object synchronize_color();

py::object texture_color_info(py::str filename_arg, py::handle read_as_is_arg, py::handle observe_texture_cache_arg);
py::list texture_cache_plan(py::handle snapshot_arg);
void texture_cache_generate(py::handle item);

py::object cop_image_source_info(py::str source_arg);

py::object cop_capture_stats(py::handle capsule_arg);

py::object cop_image_sources(py::handle capsule_arg);

py::object freeze_cop_image(py::str source_arg, py::handle time_arg);

py::object cop_image_info(py::handle value_arg);

py::object cop_image_pixels(py::handle value_arg);

ch::DisplaySettings read_display_settings(py::handle data);

py::object display_image(py::handle capsule_arg, py::handle data_arg);

py::object display_gpu_input(py::handle capsule_arg, py::handle data_arg, py::str known_key_arg,
                             py::handle copy_pixels_arg);

py::object expand_display_rgba(py::handle input_arg, py::handle source_width_arg, py::handle source_height_arg,
                               py::handle width_arg, py::handle height_arg);

py::list camera_matrix_value(const ch::Matrix &matrix);

py::object resolve_camera_motion(py::handle nominal_arg, py::handle sample_values_arg, py::handle shutter_arg,
                                 py::str source_arg, py::handle vectors_arg, py::handle width_arg,
                                 py::handle height_arg, py::handle pixel_aspect_arg);

py::object compact_frame_fixture(bool full_coverage);
py::object display_frame_fixture(py::handle pixels_arg, py::handle width_arg, py::handle height_arg);

py::object output_metadata(py::handle capsule_arg);

py::object open_display(py::handle capsule_arg, py::str destination_arg, py::str socket_arg,
                        const std::map<std::string, std::string> &options);
bool display_ready(py::handle capsule_arg);
bool set_native_display_status(py::handle capsule_arg, std::string message);

py::object present(py::handle capsule_arg);

py::object test_present_frame_rendezvous(py::handle capsule_arg, py::handle image_arg, py::handle entered_arg,
                                         py::handle release_arg);

py::object close_display(py::handle capsule_arg);

py::object process_memory_json();

py::object shader_catalogue_json();

py::object shader_curve_table(py::handle source_arg, py::str kind_arg);

py::object ramp_preview_sample(py::handle capsule_arg, py::handle factor_arg);

py::object ramp_preview(py::handle color_arg, py::handle alpha_arg, py::handle factors_arg, py::handle options_arg);

py::object vop_input_activity(py::handle id_arg, py::handle time_arg, py::handle values_arg);

py::object vop_input_controls(py::handle id_arg);

py::object vop_connector_layout(py::handle id_arg);

py::object vop_output_override(py::handle id_arg, py::str output_arg);

py::object textures_changed(py::handle capsule_arg);

py::list texture_inputs_value(const std::vector<ch::TextureInputInfo> &inputs);

void point_instance_status(py::dict result, std::array<std::uint64_t, 3> counts, double seconds);

// Test/diagnostic hook: the synthetic packed table of one instancing request at one time
// (prototype slot and full packed transform, row-major, row vectors; instancer OBJ excluded).
py::object point_instance_table(py::handle data_arg, py::handle time_arg);

py::object texture_inputs(py::handle capsule_arg);

py::object engine_create(py::handle capture_arg);

py::object apply_snapshot(py::handle capsule_arg, py::handle desired_arg, py::handle ack_arg);

py::object owned_frame(py::handle capsule_arg, py::handle after_arg);

py::object present_frame(py::handle capsule_arg, py::handle image_arg, py::handle sequence_arg);
py::object test_wait_display(py::handle capsule_arg);

py::bytes bytes_value(const std::vector<std::byte> &bytes);

// Color is the only scene input to worker initialization. Keep it immutable
// while the host captures geometry and the controller opens the device.
py::object worker_startup_snapshot();

py::object encode_config(py::handle capture_arg, py::handle desired_arg, py::str root_arg);

py::object encode_color_configuration();

py::object snapshot_color_identity(py::handle capsule_arg);

extern std::atomic<std::uint64_t> next_root_revision;

py::object prepare_navigation(py::handle capsule_arg, py::handle baseline_arg, py::handle data_arg, py::handle time_arg,
                              py::handle previous_arg);

py::object prepare_object_navigation(py::handle capture_arg, py::handle baseline_arg, py::handle data_arg,
                                     py::handle time_arg, py::handle previous_arg);

py::object navigation_info(py::handle capsule_arg);

py::object navigation_snapshot(py::handle capsule_arg, py::handle navigation_arg, py::handle keep_unchanged_arg,
                               py::handle camera_only_arg);

py::object navigation_basis_equal(py::handle before_arg, py::handle after_arg);
py::object navigation_stats();
py::object release_navigation_memo();

py::object navigation_context_equal(py::handle capsule_arg, py::handle navigation_arg);

py::object snapshot_info(py::handle capsule_arg, py::handle ack_arg);
py::object snapshot_digest(py::handle capsule);

py::object encode_snapshot(py::handle desired_arg, py::handle ack_arg, py::handle reuse_mesh_payloads_arg,
                           py::handle root_poses_arg);
void bind_wire_transport(py::module_ &m); // B1 WirePacket (python_snapshot.cpp)

py::dict reply_value(py::handle packet, const std::shared_ptr<const ch::ColorConfiguration> &color);

py::object decode_reply(py::handle packet_arg, py::handle desired_arg);

py::object frame_receiver();

py::object decode_render_reply(py::handle packet_arg, py::handle receiver_arg);

py::object encode_render_reply(py::handle capsule_arg, py::handle after_arg);

py::object set_preview_pass(py::handle capsule_arg, py::str pass_arg, py::handle focus_arg);

py::object focus_frame(py::handle capsule_arg);

py::object sample_focus(py::handle capsule_arg, py::handle x_arg, py::handle y_arg);

extern std::atomic<int> geometry_leases;

py::object preserve_geometry(py::str path_arg, py::handle time_arg, py::handle copy_arg, py::handle material_arg);

py::object convert_preserved_mesh(py::handle capsule_arg);

// Internal owned-data observation: calls the production curve converter on a
// frozen detail, without evaluating an OP or mutating source geometry.
py::object convert_preserved_curves(py::handle capsule_arg, py::handle divisions_arg, py::handle velocity_arg,
                                    py::handle names_arg);

py::object preserve_deformation(py::str path_arg, py::handle time_arg, py::handle fps_arg, py::handle shutter_arg,
                                py::handle samples_arg, py::handle material_arg);

py::object convert_preserved_deformation(py::handle capsule_arg);

// Native typed-volume fixtures exercise representations that the legacy
// Volume SOP authors as separate scalar components instead of collated data.
py::object write_dense_volume_fixture(py::str path_arg, py::handle mode_arg);

// A gridless VDB cannot safely pass through Houdini's geometry serializer.
// Exercise its native ownership/conversion boundary directly, then repair it.
py::object probe_gridless_volume();

py::object preserve_volume(py::str path_arg, py::handle time_arg);

py::object convert_preserved_volume(py::handle capsule_arg);

py::object preserve_packed_geometry(py::str path_arg, py::handle time_arg, py::handle material_arg);
py::list packed_blender_references(py::str path_arg, double time);

py::object convert_preserved_packed_geometry(py::handle capsule_arg);

py::object preserve_material_graphs(py::handle paths_arg, py::handle resolver_arg, py::handle time_arg);

py::object shader_value_object(const ch::ShaderValue &value);

py::dict graph_value(const ch::ShaderGraphData &graph);

py::object convert_preserved_material_graphs(py::handle capsule_arg, py::handle paths_arg);

// Diagnostic entry points exercise the production geometry boundary. The
// baseline owns camera/color/texture policy; they are not an artist scheduler.
py::object preserve_native_scene(py::handle data_arg, py::handle time_arg, py::handle baseline_arg);

py::object convert_preserved_native_scene(py::handle capsule_arg);

py::object pass_catalogue();

} // namespace cycles_houdini::python_detail
