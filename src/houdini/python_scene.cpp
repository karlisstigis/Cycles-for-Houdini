// SPDX-License-Identifier: Apache-2.0
#include "python_binding_state.h"
#include <pybind11/stl.h>
#include <chrono>
#include <cmath>
#include <cstring>
#include <limits>
#include "preserved_detail.h"
#include "blend_mesh.h"
#include "mesh_adapter.h"
#include "point_curve_adapter.h"
#include "geometry_selection.h"
#include "deformation_adapter.h"
#include "volume_adapter.h"
#include "packed_adapter.h"
#include "packed_blender_reference.h"
#include "cycles_houdini/volume.h"
#include "../core/color_internal.h"
#include "../core/camera_motion.h"
#include "../core/render_settings.h"
#include <functional>
#include <type_traits>
#include <GU/GU_Detail.h>
#include <GU/GU_PrimPacked.h>
#include <GU/GU_PackedGeometry.h>
#include <GU/GU_PackedFragment.h>
#include <GU/GU_PrimVDB.h>
#include <GU/GU_PrimVolume.h>
#include <GA/GA_Iterator.h>
#include <CH/CH_Manager.h>
#include <OP/OP_Context.h>
#include <OP/OP_Director.h>
#include <OBJ/OBJ_Camera.h>
#include <SOP/SOP_Node.h>
#include <UT/UT_XXHash.h>
#include <UT/UT_Thread.h>

namespace cycles_houdini::python_detail {
struct ApplyJob {
  Handle *handle{};
  py::object owner;
  std::shared_ptr<ch::NativeSceneAdapter::PreparedScene> geometry;
  std::shared_ptr<ch::NativeSceneRoots> roots;
  std::shared_ptr<const ch::ColorConfiguration> color;
  ch::NativeMaterialGraphTable graphs;
  ch::SceneUpdate update;
  std::vector<ch::NativeMaterialReference> references;
  std::set<std::string> native_keys;
  std::shared_ptr<const ch::SceneSnapshotState> state;
  std::shared_ptr<const ch::NativeNavigationState> navigation;
  std::shared_ptr<const ch::SceneSnapshotState> image_state;
  std::shared_ptr<ch::NativeCopBatch> images;
  ch::NativePreparationBudget preparation;
  bool preparation_started{};
  bool select_native{}, reuse{}, finished{}, released{}, touched_adapter{};
  std::atomic<int> phase{0}; // prepared, converting, converted, failed
  double conversion_seconds{};
};

struct GeometryLease {
  ch::PreservedDetail detail;
  std::string path;
  ch::GeometryRequest request;
  double cook_seconds{}, acquire_seconds{};
  std::uint64_t source_id{};
  std::array<long long, 3> source_data_ids{};
  GeometryLease(GU_DetailHandle value, std::string source) : detail(std::move(value)), path(std::move(source)) {
    ++geometry_leases;
  }
  ~GeometryLease() { --geometry_leases; }
};

struct DeformationLease {
  std::shared_ptr<const ch::PreparedDeformation> prepared;
  ch::ExtractionStats preparation;
  double seconds{}, sample_seconds{};
  DeformationLease() { ++geometry_leases; }
  ~DeformationLease() { --geometry_leases; }
};

struct VolumeLease {
  std::shared_ptr<const ch::PreparedVolume> prepared;
  double cooking_seconds{}, acquire_seconds{};
  VolumeLease() { ++geometry_leases; }
  ~VolumeLease() { --geometry_leases; }
};

struct PackedLease {
  struct Leaf {
    std::shared_ptr<const ch::PreservedDetail> detail;
    ch::GeometryRequest request;
    ch::Matrix transform;
    std::string identity;
  };
  ch::NativePackedSources sources;
  std::vector<Leaf> leaves;
  double cooking_seconds{}, acquire_seconds{};
  PackedLease() { ++geometry_leases; }
  ~PackedLease() { --geometry_leases; }
};

struct MaterialGraphLease {
  ch::NativeMaterialLookup candidates;
  ch::NativeMaterialGraphTable graphs;
  MaterialGraphLease() { ++geometry_leases; }
  ~MaterialGraphLease() { --geometry_leases; }
};

struct NativeSceneLease {
  ch::NativeSceneAdapter adapter;
  std::shared_ptr<ch::NativeSceneAdapter::PreparedScene> prepared;
  ch::NativeMaterialGraphTable graphs;
  SnapshotPtr baseline;
  double prepare_seconds{}, cook_seconds{};
  std::atomic<bool> consumed{};
  NativeSceneLease() { ++geometry_leases; }
  ~NativeSceneLease() { --geometry_leases; }
};

void release_apply_payload(ApplyJob &job);

void prepare_apply_data(ApplyJob &job, Handle &h, py::handle data, double time, bool reuse_if_unchanged);

void convert_apply_data(ApplyJob &job);

bool acquire_apply_images(ApplyJob &job);

void convert_apply_images(ApplyJob &job);

py::object finish_apply_data(ApplyJob &job);

ApplyJob &apply_job(py::handle capsule);

ch::GeometryRequest geometry_request(py::handle value, int depth) {
  {
    ch::GeometryRequest r;
    r.sop_path = string(item(value, "sop"));
    r.geometry_key = string(item(value, "geometry_key"));
    r.object_key = string(item(value, "object_key"));
    if (auto v = item(value, "material", false))
      r.material_key = string(v);
    if (auto v = item(value, "transform", false))
      r.transform = matrix(v);
    if (auto v = item(value, "material_mode", false)) {
      const auto mode = string(v);
      if (mode == "native")
        r.material_mode = ch::NativeMaterialMode::Native;
      else if (mode == "uniform")
        r.material_mode = ch::NativeMaterialMode::Uniform;
      else if (mode != "reject")
        throw std::invalid_argument("Unsupported native material assignment mode: " + mode);
    }
    if (auto v = item(value, "point_splat_default_material", false))
      r.point_splat_default_material = int(truth(v)) > 0;
    if (auto v = item(value, "point_render_mode", false)) {
      const auto mode = string(v);
      if (mode == "auto")
        r.point_render_mode = ch::NativePointRenderMode::Auto;
      else if (mode == "spheres")
        r.point_render_mode = ch::NativePointRenderMode::Spheres;
      else if (mode == "gaussian_splats")
        r.point_render_mode = ch::NativePointRenderMode::GaussianSplats;
      else
        throw std::invalid_argument("Unsupported point render mode: " + mode);
    }
    if (auto v = item(value, "object_material_path", false))
      r.object_material_path = string(v);
    if (auto v = item(value, "object_material_overrides", false))
      for (auto layer : sequence(v))
        r.object_material_overrides.push_back(string(layer));
    if (auto v = item(value, "object_surface_overrides", false))
      for (auto layer : sequence(v))
        r.object_surface_overrides.push_back(string(layer));
    if (auto v = item(value, "object_displacement_overrides", false))
      for (auto layer : sequence(v))
        r.object_displacement_overrides.push_back(string(layer));
    if (auto v = item(value, "stylesheet", false))
      r.stylesheet = string(v);
    if (auto v = item(value, "stylesheet_errors", false))
      for (auto error : sequence(v))
        r.stylesheet_errors.push_back(string(error));
    if (auto v = item(value, "stylesheet_resources", false))
      for (auto resource : sequence(v))
        r.stylesheet_resources.push_back(string(resource));
    if (auto v = item(value, "object_material_revision", false))
      r.object_material_revision = string(v);
    if (auto v = item(value, "object_material_samples", false)) {
      const auto values = sequence(v);
      if (!values.empty() && (values.size() < 3 || values.size() > 129 || values.size() % 2 == 0))
        throw std::invalid_argument("Owned object material samples require an odd 3..129 shutter list");
      ch::GeometryRequest::MaterialSamples::Values samples;
      std::size_t bytes = 0;
      for (auto value : values) {
        ch::GeometryRequest::MaterialSample sample;
        sample.time = number(item(value, "time"));
        if (!std::isfinite(sample.time) || (!samples.empty() && sample.time <= samples.back().time))
          throw std::invalid_argument("Owned object material sample times must be finite and increasing");
        sample.assignment.path = string(item(value, "object_material_path"));
        for (auto field : sequence(item(value, "object_material_overrides")))
          sample.assignment.overrides.push_back(string(field));
        sample.stylesheet = string(item(value, "stylesheet"));
        for (auto field : sequence(item(value, "stylesheet_errors")))
          sample.errors.push_back(string(field));
        for (auto field : sequence(item(value, "stylesheet_resources")))
          sample.resources.push_back(string(field));
        bytes += sample.stylesheet.size() + sample.assignment.path.size();
        for (const auto *fields : {&sample.assignment.overrides, &sample.errors, &sample.resources})
          for (const auto &text : *fields)
            bytes += text.size();
        if (bytes > 8 * 1024 * 1024)
          throw std::invalid_argument("Owned object material samples exceed 8 MiB");
        samples.push_back(std::move(sample));
      }
      if (!samples.empty())
        r.object_material_samples.values =
            std::make_shared<const ch::GeometryRequest::MaterialSamples::Values>(std::move(samples));
    }
    if (auto v = item(value, "smooth", false))
      r.smooth = int(truth(v)) > 0;
    if (auto settings = item(value, "subdivision", false)) {
      auto &s = r.subdivision;
      if (auto field = item(settings, "adaptive", false)) {
        if (!py::isinstance<py::bool_>(field))
          throw std::invalid_argument("Subdivision adaptive must be boolean");
        s.adaptive = field.is(py::bool_(true));
      }
      if (auto field = item(settings, "cache_topology", false)) {
        if (!py::isinstance<py::bool_>(field))
          throw std::invalid_argument("Subdivision cache_topology must be boolean");
        s.cache_topology = field.is(py::bool_(true));
      }
      if (auto field = item(settings, "use_creases", false)) {
        if (!py::isinstance<py::bool_>(field))
          throw std::invalid_argument("Subdivision use_creases must be boolean");
        s.use_creases = field.is(py::bool_(true));
      }
      const auto choice = [&](const char *name, const std::vector<std::string> &values, int fallback) {
        const auto field = item(settings, name, false);
        if (!field)
          return fallback;
        const auto token = string(field);
        const auto found = std::find(values.begin(), values.end(), token);
        if (found == values.end())
          throw std::invalid_argument(std::string("Unsupported subdivision ") + name + ": " + token);
        return int(found - values.begin());
      };
      s.type = static_cast<ch::SubdivisionType>(choice("type", {"none", "linear", "catmull_clark"}, int(s.type)));
      s.space = static_cast<ch::SubdivisionSpace>(choice("space", {"object", "pixel"}, int(s.space)));
      s.boundary = static_cast<ch::SubdivisionBoundary>(
          choice("boundary", {"none", "edge_only", "edge_and_corner"}, int(s.boundary)));
      s.fvar = static_cast<ch::SubdivisionFVar>(
          choice("fvar", {"none", "corners_only", "corners_plus1", "corners_plus2", "boundaries", "all"}, int(s.fvar)));
      if (auto field = item(settings, "dicing_rate", false))
        s.dicing_rate = float(number(field));
      if (auto field = item(settings, "max_level", false)) {
        const auto level = integer<long>(field, "Subdivision maximum level must be an integer from 0 to 16");
        if (level < 0 || level > 16)
          throw std::invalid_argument("Subdivision maximum level must be an integer from 0 to 16");
        s.max_level = int(level);
      }
      if (auto field = item(settings, "triangle_limit", false)) {
        const auto limit = integer<unsigned long long>(
            field, "Subdivision triangle budget must be a nonnegative integer; zero disables it");

        s.triangle_limit = limit;
      }
      if (!std::isfinite(s.dicing_rate) || s.dicing_rate <= 0)
        throw std::invalid_argument("Subdivision dicing rate must be finite and positive");
    }
    if (auto v = item(value, "point_radius", false))
      r.point_radius = float(number(v));
    if (auto v = item(value, "curve_width", false))
      r.curve_width = float(number(v));
    if (auto v = item(value, "curve_segments", false)) {
      const auto count = integer<long>(v, "Curve Segments per Span must be an integer from 1 to 256");
      if (count < 1 || count > 256)
        throw std::invalid_argument("Curve Segments per Span must be an integer from 1 to 256");
      r.curve_segments = int(count);
    }
    if (auto v = item(value, "curve_shape", false)) {
      const auto name = string(v);
      if (name == "thick")
        r.curve_shape = ch::CurveShape::Thick;
      else if (name == "ribbon")
        r.curve_shape = ch::CurveShape::Ribbon;
      else if (name != "linear")
        throw std::invalid_argument("Unsupported curve shape: " + name);
    }
    if (auto v = item(value, "render_types", false)) {
      int index = 0;
      for (const auto *name : {"mesh", "points", "curves", "volume", "packed"}) {
        if (auto flag = item(v, name, false)) {
          if (!py::isinstance<py::bool_>(flag))
            throw std::invalid_argument("Render type flags must be boolean");
          r.render_types[index] = flag.is(py::bool_(true));
        }
        ++index;
      }
    }
    if (auto v = item(value, "object_properties", false)) {
      auto &p = r.properties;
      if (auto field = item(v, "holdout", false))
        p.holdout = int(truth(field)) > 0;
      if (auto field = item(v, "pass_index", false))
        p.pass_index = pass_index(field);
      if (auto field = item(v, "dielectric_priority", false))
        p.dielectric_priority = dielectric_priority(field);
      if (auto field = item(v, "shadow_catcher", false)) {
        if (!py::isinstance<py::bool_>(field))
          throw std::invalid_argument("Shadow Catcher requires a boolean");
        p.shadow_catcher = field.is(py::bool_(true));
      }
      if (auto field = item(v, "caustics_caster", false))
        p.caustics_caster = int(truth(field)) > 0;
      if (auto field = item(v, "caustics_receiver", false))
        p.caustics_receiver = int(truth(field)) > 0;
      if (auto field = item(v, "shadow_terminator_shading", false))
        p.shadow_terminator_shading = float(number(field));
      if (auto field = item(v, "shadow_terminator_geometry", false))
        p.shadow_terminator_geometry = float(number(field));
      if (auto field = item(v, "ao_distance", false))
        p.ao_distance = float(number(field));
      if (auto field = item(v, "lightgroup", false))
        p.lightgroup = shader_string(field);
      if (auto field = item(v, "linking_id", false))
        p.linking_id = string(field);
      if (auto field = item(v, "cryptomatte_object", false))
        p.cryptomatte_object = shader_string(field);
      if (auto field = item(v, "cryptomatte_asset", false))
        p.cryptomatte_asset = shader_string(field);
      ch::validate_object_properties(p);
    }
    if (auto v = item(value, "volume_object_space", false)) {
      if (!py::isinstance<py::bool_>(v))
        throw std::invalid_argument("Volume object-space density must be boolean");
      r.volume_object_space = v.is(py::bool_(true));
    }
    if (auto v = item(value, "volume_step_size", false))
      r.volume_step_size = float(number(v));
    if (auto v = item(value, "object_path", false))
      r.object_path = string(v);
    if (auto v = item(value, "motion", false)) {
      const auto mode = string(v);
      if (mode == "velocity")
        r.motion = ch::NativeGeometryMotion::Velocity;
      else if (mode == "deformation")
        r.motion = ch::NativeGeometryMotion::Deformation;
      else if (mode == "acceleration")
        r.motion = ch::NativeGeometryMotion::Acceleration;
      else if (mode == "velocity_if_available")
        r.motion = ch::NativeGeometryMotion::VelocityIfAvailable;
      else if (mode != "transform")
        throw std::invalid_argument("Unsupported native geometry motion mode: " + mode);
    }
    if (auto v = item(value, "motion_blur", false)) {
      if (!py::isinstance<py::bool_>(v))
        throw std::invalid_argument("Object motion_blur must be boolean");
      r.motion_blur = v.is(py::bool_(true));
    }
    if (auto v = item(value, "motion_explicit", false)) {
      if (!py::isinstance<py::bool_>(v))
        throw std::invalid_argument("Object motion_explicit must be boolean");
      r.motion_explicit = v.is(py::bool_(true));
    }
    if (!std::isfinite(r.point_radius) || r.point_radius < 0 || !std::isfinite(r.curve_width) || r.curve_width < 0)
      throw std::invalid_argument("Default point radius and curve width must be finite and nonnegative");
    if (!std::isfinite(r.volume_step_size) || r.volume_step_size < 0)
      throw std::invalid_argument("Volume step size must be finite and nonnegative");
    if (auto v = item(value, "random_id", false)) {
      const auto id = integer<unsigned long>(v, "Invalid random ID");

      r.random_id = static_cast<std::uint32_t>(id);
    }
    if (auto v = item(value, "visibility", false)) {
      const auto mask = integer<unsigned long>(v, "Native ray visibility requires an unsigned 8-bit mask");
      if (mask > 255)
        throw std::invalid_argument("Native ray visibility requires an unsigned 8-bit mask");
      r.visibility = static_cast<std::uint32_t>(mask);
    }
    for (const auto *role : {"point_instancing", "packed_blender"}) {
      auto v = item(value, role, false);
      if (!v)
        continue;
      // Instance OBJ / s@instance: prototype requests are parsed recursively (nested instancers).
      if (depth >= 32)
        throw std::invalid_argument("Point instancing is nested more than 32 levels");
      auto spec = std::make_shared<ch::NativePointInstancing>();
      const auto mode = string(item(v, "mode"));
      if (mode == "fast")
        spec->mode = ch::NativeInstancingMode::Fast;
      else if (mode == "full")
        spec->mode = ch::NativeInstancingMode::Full;
      else if (mode == "off")
        spec->mode = ch::NativeInstancingMode::Off;
      else if (mode == "members")
        spec->mode = ch::NativeInstancingMode::Members;
      else
        throw std::invalid_argument("Unsupported point instancing mode: " + mode);
      spec->instancer_path = string(item(v, "instancer_path"));
      if (auto frame = item(v, "frame", false)) {
        spec->frame = number(frame);
        if (!std::isfinite(spec->frame))
          throw std::invalid_argument("Point instancing frame must be finite");
      }
      if (auto samples = item(v, "frame_samples", false)) {
        const auto values = sequence(samples);
        if (values.size() > 130)
          throw std::invalid_argument("Point instancing accepts at most 130 source frame samples");
        for (auto sample : values) {
          const double time = number(item(sample, "time")), frame = number(item(sample, "frame"));
          if (!std::isfinite(time) || !std::isfinite(frame) || !spec->frame_samples.emplace(time, frame).second)
            throw std::invalid_argument("Point instancing frame samples must have unique finite times and frames");
        }
      }
      const auto fallback = integer<long>(item(v, "fallback"), "Invalid point instancing fallback");

      for (auto entry : sequence(item(v, "prototypes"))) {
        ch::NativePointPrototype prototype;
        prototype.path = string(item(entry, "path"));
        prototype.node_id = integer<long long>(item(entry, "node_id"), "Invalid prototype node id");

        if (auto reference = item(entry, "source_reference", false))
          prototype.source_reference = string(reference);
        if (auto frame = item(entry, "source_frame", false)) {
          prototype.source_frame = number(frame);
          if (!std::isfinite(*prototype.source_frame))
            throw std::invalid_argument("External prototype frame must be finite");
        }
        if (prototype.source_reference.empty() != !prototype.source_frame)
          throw std::invalid_argument("External prototype requires both source_reference and source_frame");
        if (!prototype.source_reference.empty() && !ch::native_blend_reference(prototype.source_reference))
          throw std::invalid_argument(
              "External Blender reference requires .blend#object= or .blend#collection= selector syntax");
        if (auto members = item(entry, "direct_instances", false)) {
          if (prototype.source_reference.empty())
            throw std::invalid_argument("Direct Blender members require an external source reference");
          for (auto member : sequence(members)) {
            auto owned = std::make_shared<ch::GeometryRequest>();
            owned->external_mesh = ch::blend_mesh_from_capsule(item(member, "mesh"));
            const auto key = string(item(member, "key"));
            owned->sop_path = prototype.path + "/" + key;
            owned->object_key = key;
            if (auto identity = item(member, "identity", false))
              owned->object_key = string(identity);
            auto geometry = item(member, "geometry_key", false);
            owned->geometry_key = geometry ? string(geometry) : owned->external_mesh->key;
            if (auto paths = item(member, "material_paths", false)) {
              for (auto path : sequence(paths))
                owned->external_material_paths.push_back(string(path));
            } else
              owned->external_material_paths = owned->external_mesh->materials;
            owned->material_mode = ch::NativeMaterialMode::Native;
            if (auto transform = item(member, "transform", false)) {
              const auto values = sequence(transform);
              if (values.size() == 12)
                owned->transform = matrix(transform);
              else if (values.size() == 16) {
                for (int row = 0; row < 3; ++row)
                  for (int col = 0; col < 4; ++col) {
                    const auto v = number(values[4 * row + col]);
                    if (!std::isfinite(v) || std::abs(v) > std::numeric_limits<float>::max())
                      throw std::invalid_argument("Direct Blender member transform must be finite");
                    owned->transform[4 * row + col] = float(v);
                  }
                for (int col = 0; col < 4; ++col)
                  if (number(values[12 + col]) != (col == 3 ? 1 : 0))
                    throw std::invalid_argument("Direct Blender member transform must be affine");
              } else
                throw std::invalid_argument("Direct Blender member transform requires 12 or 16 values");
            }
            if (owned->external_mesh->subdivision)
              owned->subdivision = owned->external_mesh->subdivision->settings;
            if (auto values = item(member, "blender_object", false)) {
              // Blender's per-object values (blender_assets._blender_object; docs/dev/blend-import.md).
              ch::BlenderObjectValues blender;
              blender.random_id = static_cast<std::uint32_t>(
                  integer<unsigned long long>(item(values, "random_id"), "Invalid Blender random ID") & 0xffffffffull);
              const auto mask = integer<unsigned long>(item(values, "visibility"), "Invalid Blender ray visibility");
              if (mask > 255)
                throw std::invalid_argument("Blender ray visibility requires an unsigned 8-bit mask");
              blender.visibility = static_cast<std::uint32_t>(mask);
              blender.holdout = int(truth(item(values, "holdout"))) > 0;
              blender.shadow_catcher = int(truth(item(values, "shadow_catcher"))) > 0;
              blender.pass_index = pass_index(item(values, "pass_index"));
              const auto color = sequence(item(values, "color"));
              if (color.size() != 4)
                throw std::invalid_argument("Blender object colour requires four values");
              blender.color = {float(number(color[0])), float(number(color[1])), float(number(color[2]))};
              blender.alpha = float(number(color[3]));
              if (!std::isfinite(blender.color.x) || !std::isfinite(blender.color.y) ||
                  !std::isfinite(blender.color.z) || !std::isfinite(blender.alpha))
                throw std::invalid_argument("Blender object colour must be finite");
              if (auto fields = item(values, "attributes", false))
                for (auto field : sequence(fields)) {
                  ch::UniformAttribute attribute;
                  attribute.name = string(item(field, "name"));
                  attribute.components = int(integer<long>(item(field, "components"), "Invalid attribute size"));
                  const auto value = sequence(item(field, "value"));
                  if (attribute.name.empty() || attribute.components < 1 || attribute.components > 3 ||
                      value.size() != std::size_t(attribute.components))
                    throw std::invalid_argument("Invalid Blender object attribute");
                  float parts[3]{};
                  for (std::size_t i = 0; i < value.size(); ++i) {
                    parts[i] = float(number(value[i]));
                    if (!std::isfinite(parts[i]))
                      throw std::invalid_argument("Blender object attribute must be finite");
                  }
                  attribute.value = {parts[0], parts[1], parts[2]};
                  blender.attributes.push_back(std::move(attribute));
                }
              owned->blender_object = std::move(blender);
            }
            prototype.external_members.push_back(std::move(owned));
          }
        }
        if (auto request = item(entry, "request", false))
          prototype.request = std::make_shared<const ch::GeometryRequest>(geometry_request(request, depth + 1));
        if (!prototype.request && prototype.source_reference.empty())
          throw std::invalid_argument("Point prototype requires a geometry request");
        spec->prototypes.push_back(std::move(prototype));
      }
      if (fallback < -1 || fallback >= long(spec->prototypes.size()))
        throw std::invalid_argument("Point instancing fallback is out of range");
      spec->fallback = int(fallback);
      if (auto keep = item(v, "keep_attributes", false))
        for (auto name : sequence(keep))
          spec->keep_attributes.push_back(string(name));
      if (std::strcmp(role, "packed_blender") == 0)
        r.packed_blender.value = std::move(spec);
      else
        r.point_instancing.value = std::move(spec);
    }
    return r;
  }
}

ch::NativeMotionSettings motion_settings(py::handle motion_data) {
  ch::NativeMotionSettings motion;
  if (motion_data) {
    if (auto v = item(motion_data, "vectors", false))
      motion.vectors = int(truth(v)) > 0;
    if (auto v = item(motion_data, "enabled", false))
      motion.enabled = int(truth(v)) > 0;
    if (auto v = item(motion_data, "fps", false))
      motion.frames_per_second = number(v);
    if (auto v = item(motion_data, "shutter_frames", false))
      motion.shutter_frames = number(v);
    if (auto v = item(motion_data, "offset", false))
      motion.shutter_offset = number(v);
    if (auto v = item(motion_data, "samples", false)) {
      const auto value = integer<long>(v, "Native motion samples must be an odd integer from 3 to 129");
      if (value < 3 || value > 129)
        throw std::invalid_argument("Native motion samples must be an odd integer from 3 to 129");
      motion.samples = int(value);
    }
  }
  return motion;
}

// A capture transaction retains only native values after host acquisition.
// Its capsule retains the handle; exclusive adapter ownership lasts until finish
// or discard. No Python callback is reachable by the conversion thread.
ch::PanoramaSettings camera_panorama(py::handle value) {
  ch::PanoramaSettings p;
  if (auto v = item(value, "type", false))
    p.type = string(v);
  const auto read = [&](const char *name, float &field) {
    if (auto v = item(value, name, false))
      field = float(number(v));
  };
  read("fisheye_fov", p.fisheye_fov);
  read("fisheye_lens", p.fisheye_lens);
  read("latitude_min", p.latitude_min);
  read("latitude_max", p.latitude_max);
  read("longitude_min", p.longitude_min);
  read("longitude_max", p.longitude_max);
  read("cylindrical_u_min", p.cylindrical_u_min);
  read("cylindrical_u_max", p.cylindrical_u_max);
  read("cylindrical_v_min", p.cylindrical_v_min);
  read("cylindrical_v_max", p.cylindrical_v_max);
  read("sensor_width", p.sensor_width);
  read("sensor_height", p.sensor_height);
  if (auto v = item(value, "sensor_fit", false))
    p.sensor_fit = string(v);
  if (auto v = item(value, "polynomial", false)) {
    const auto values = sequence(v);
    if (values.size() != 5)
      throw std::invalid_argument("Fisheye polynomial requires five coefficients");
    for (int i = 0; i < 5; ++i)
      p.polynomial[i] = float(number(values[i]));
  }
  return p;
}

ch::CameraSampling camera_sampling(py::handle value) {
  ch::CameraSampling result;
  if (auto v = item(value, "rolling_shutter", false)) {
    if (!py::isinstance<py::bool_>(v))
      throw std::invalid_argument("Camera rolling_shutter requires a boolean");
    result.rolling_shutter = v.is(py::bool_(true));
  }
  if (auto v = item(value, "rolling_duration", false))
    result.rolling_duration = float(number(v));
  if (auto v = item(value, "shutter_curve", false)) {
    if (py::len(v) > 256)
      throw std::invalid_argument("Too many shutter curve weights");
    for (auto weight : sequence(v))
      result.shutter_curve.push_back(float(number(weight)));
  }
  return result;
}

void prepare_camera_update(ch::SceneUpdate &update, py::handle data, const ch::NativeMotionWindow &window, int width,
                           int height) {
  if (auto value = item(data, "dicing", false)) {
    ch::DicingSettings settings;
    if (auto v = item(value, "offscreen_scale", false))
      settings.offscreen_scale = float(number(v));
    if (auto v = item(value, "update", false)) {
      if (!py::isinstance<py::bool_>(v))
        throw std::invalid_argument("Tessellation update requires a boolean");
      settings.update = v.is(py::bool_(true));
    }
    if (auto v = item(value, "topology_cache_mib", false))
      settings.topology_cache_mib =
          integer<std::uint32_t>(v, "IPR Topology Cache Limit must be a nonnegative integer in MiB");
    if (auto v = item(value, "refresh_revision", false)) {
      settings.refresh_revision =
          integer<unsigned long long>(v, "Tessellation refresh revision must be a nonnegative integer");
    }
    if (auto v = item(value, "camera_path", false); v && !string(v).empty()) {
      const auto path = string(v);
      if (!dynamic_cast<OBJ_Camera *>(OPgetDirector()->findNode(path.c_str())))
        throw std::invalid_argument("Dicing Camera must reference a camera OBJ: " + path);
      settings.camera =
          ch::capture_camera(path, window, width, height,
                             item(data, "pixel_aspect", false) ? float(number(item(data, "pixel_aspect"))) : 1);
    } else if (auto v = item(value, "camera", false); v && !v.is_none()) {
      py::dict header = py::dict();
      header["camera"] = v;
      ch::SceneUpdate camera;
      prepare_camera_update(camera, header, window, width, height);
      settings.camera = std::move(camera.camera);
    }
    update.dicing = std::move(settings);
  }

  if (auto path = item(data, "camera_path", false))
    update.camera =
        ch::capture_camera(string(path), window, width, height,
                           item(data, "pixel_aspect", false) ? float(number(item(data, "pixel_aspect"))) : 1);
  if (auto camera = item(data, "camera", false)) {
    ch::CameraData c;
    if (auto v = item(camera, "transform", false))
      c.transform = matrix(v);
    if (auto v = item(camera, "fov", false))
      c.short_axis_fov = float(number(v));
    if (auto v = item(camera, "near", false))
      c.near_clip = float(number(v));
    if (auto v = item(camera, "far", false))
      c.far_clip = float(number(v));
    if (auto v = item(camera, "orthographic", false))
      c.orthographic = int(truth(v)) > 0;
    if (auto v = item(camera, "ortho_scale", false))
      c.ortho_scale = float(number(v));
    if (window.shutter_frames > 0)
      c.shutter_frames = float(window.shutter_frames);
    if (auto v = item(camera, "shutter_frames", false))
      c.shutter_frames = float(number(v));
    if (auto list = item(camera, "motion", false))
      for (auto value : sequence(list))
        c.motion.push_back(matrix(value));
    if (auto samples = item(camera, "intrinsic_motion", false)) {
      if (py::len(samples) > 129)
        throw std::invalid_argument("Too many camera intrinsic samples");
      for (auto value : sequence(samples)) {
        ch::CameraIntrinsicSample sample;
        sample.short_axis_fov = float(number(item(value, "fov")));
        sample.near_clip = float(number(item(value, "near")));
        sample.far_clip = float(number(item(value, "far")));
        sample.ortho_scale = float(number(item(value, "ortho_scale")));
        if (auto p = item(value, "panorama", false); p && !p.is_none())
          sample.panorama = camera_panorama(p);
        c.intrinsic_motion.push_back(std::move(sample));
      }
    }
    if (auto sampling = item(camera, "sampling", false))
      c.sampling = camera_sampling(sampling);
    if (auto panorama = item(camera, "panorama", false); panorama && !panorama.is_none())
      c.panorama = camera_panorama(panorama);
    if (auto issues = item(camera, "source_diagnostics", false))
      c.source_diagnostics = source_issues(issues);
    update.camera = c;
  }
  if (auto lens = item(data, "camera_dof", false)) {
    if (!update.camera)
      throw std::invalid_argument("Depth of field requires a camera update");
    auto &c = *update.camera;
    if (auto v = item(lens, "aperture_size", false))
      c.aperture_size = float(number(v));
    if (auto v = item(lens, "focus_distance", false))
      c.focus_distance = float(number(v));
    if (auto v = item(lens, "aperture_rotation", false))
      c.aperture_rotation = float(number(v));
    if (auto v = item(lens, "aperture_ratio", false))
      c.aperture_ratio = float(number(v));
    if (auto v = item(lens, "aperture_blades", false))
      c.aperture_blades = world_integer(v);
    if (auto v = item(lens, "image_iris", false))
      c.image_iris = string(v);
    if (auto v = item(lens, "image_iris_method", false))
      c.image_iris_method = world_integer(v);
    if (auto v = item(lens, "image_iris_color", false))
      c.image_iris_color = int(truth(v)) > 0;
    if (auto samples = item(lens, "motion", false)) {
      const auto count = py::len(samples);
      if (count < 0 || count > 129)
        throw std::invalid_argument("Too many camera depth-of-field samples");
      for (auto value : sequence(samples))
        c.dof_motion.push_back(
            {float(number(item(value, "aperture_size"))), float(number(item(value, "focus_distance"))),
             float(number(item(value, "aperture_rotation"))), float(number(item(value, "aperture_ratio")))});
    }
    if (auto v = item(lens, "source_diagnostics", false)) {
      auto issues = source_issues(v);
      c.source_diagnostics.insert(c.source_diagnostics.end(), issues.begin(), issues.end());
    }
  }
  if (auto sampling = item(data, "camera_sampling", false)) {
    if (!update.camera)
      throw std::invalid_argument("Camera sampling requires a camera update");
    update.camera->sampling = camera_sampling(sampling);
    if (auto v = item(sampling, "source_diagnostics", false)) {
      auto issues = source_issues(v);
      update.camera->source_diagnostics.insert(update.camera->source_diagnostics.end(), issues.begin(), issues.end());
    }
  }
  if (update.camera) {
    if (auto v = item(data, "pixel_aspect", false))
      update.camera->pixel_aspect = float(number(v));
    if (auto path = item(data, "camera_path", false))
      ch::resolve_camera_source_optics(*update.camera, string(path));
    else if (auto camera = item(data, "camera", false))
      if (auto path = item(camera, "source_path", false))
        ch::resolve_camera_source_optics(*update.camera, string(path));
  }
}

std::vector<std::string> read_pass_names(py::handle value) {
  if (py::isinstance<py::str>(value))
    throw std::invalid_argument("Pass names must be a sequence of strings");
  std::vector<std::string> result;
  for (auto name : sequence(value))
    result.push_back(string(name));
  ch::validate_passes(result);
  return result;
}

void release_apply_payload(ApplyJob &job) {
  // Explicit retirement keeps the capsule's last-reference destruction cheap,
  // regardless of which thread later drops a completed Future or job wrapper.
  const auto release = [](auto &value) { std::remove_reference_t<decltype(value)>{}.swap(value); };
  job.geometry.reset();
  job.roots.reset();
  release(job.graphs);
  job.update = {};
  release(job.references);
  release(job.native_keys);
  job.state.reset();
  job.navigation.reset();
  job.image_state.reset();
  job.images.reset();
  job.color.reset();
}

// What a material asks the geometry capture to carry (attribute names) and whether it asks for pointiness.
// One definition for the complete read and for prepare_material_edit.
std::set<std::string> material_geometry_fields(const ch::MaterialData &material, bool displacement_only,
                                               bool nested_dielectrics) {
  std::set<std::string> result;
  if (material.color_attribute == "Cd")
    result.insert("Cd");
  if (material.graph) {
    if (!displacement_only) {
      result = ch::shader_attribute_requests(*material.graph);
      // Volume-field names too: a mesh attribute of such a name is carried only
      // so the core can note that a medium does not read it (as in Blender).
      const auto volume = ch::shader_volume_attribute_requests(*material.graph);
      result.insert(volume.begin(), volume.end());
    } else if (material.graph->displacement && material.graph->displacement_method != ch::DisplacementMethod::Bump)
      result = ch::shader_attribute_requests(ch::displacement_snapshot(*material.graph));
  }
  for (const auto &sample : material.displacement_samples) {
    const auto fields = ch::shader_attribute_requests(sample);
    result.insert(fields.begin(), fields.end());
  }
  if (nested_dielectrics && !displacement_only)
    result.insert(ch::dielectric_priority_attribute);
  return result;
}
bool material_geometry_pointiness(const ch::MaterialData &material, bool displacement_only,
                                  const std::set<std::string> &active_aovs) {
  if (material.graph) {
    if (!displacement_only && ch::shader_pointiness_request(*material.graph, active_aovs))
      return true;
    if (displacement_only && material.graph->displacement &&
        material.graph->displacement_method != ch::DisplacementMethod::Bump &&
        ch::shader_pointiness_request(ch::displacement_snapshot(*material.graph)))
      return true;
  }
  for (const auto &sample : material.displacement_samples)
    if (ch::shader_pointiness_request(sample))
      return true;
  return false;
}

void prepare_apply_data(ApplyJob &job, Handle &h, py::handle data, double time, bool reuse_if_unchanged) {
  job.handle = &h;
  job.reuse = reuse_if_unchanged;
  if (!job.preparation_started) {
    job.preparation_started = true;
  }
  // A known cooperative continuation re-enters preparation against the same
  // owned roots. Attempt-local graph callbacks and updates never accumulate.
  job.geometry.reset();
  job.graphs.clear();
  job.update = {};
  job.references.clear();
  job.native_keys.clear();
  job.state.reset();
  job.navigation.reset();
  job.image_state.reset();
  job.images.reset();
  if (!job.color)
    job.color = ch::current_color_configuration();
  auto motion_data = item(data, "motion", false);
  const auto motion = motion_settings(motion_data);
  const auto window = ch::native_motion_window(time, motion);
  auto requests = geometry_requests(item(data, "geometry", false));
  // Freeze uniform material values before selecting demanded geometry fields.
  if (auto list = item(data, "materials", false))
    for (auto value : sequence(list)) {
      auto m = material(value);
      const auto context = [time](ch::ShaderGraphData &graph) {
        if (!graph.image_time && std::any_of(graph.nodes.begin(), graph.nodes.end(), [](const auto &node) {
              const auto file = node.parameters.find("filename");
              return file != node.parameters.end() && std::holds_alternative<std::string>(file->second) &&
                     std::get<std::string>(file->second).starts_with("op:");
            }))
          graph.image_time = time;
      };
      if (m.graph)
        context(*m.graph);
      for (auto &sample : m.displacement_samples)
        context(sample);
      if (m.key.starts_with("native-material:"))
        throw std::invalid_argument("Native material keys are owned by the assignment resolver");
      job.update.materials.push_back(std::move(m));
    }
  // Both execution modes commit an immutable texture/material state. The
  // owned-snapshot handle is capture-only; direct partial updates must retain
  // uniform shader field demands and rendering switches from texture_state.
  const auto &committed_state = h.capture_only ? h.desired : h.texture_state;
  // Nested dielectrics: packed instances may override Dielectric Priority with a
  // uniform cycles_dielectric_priority field. Request it only while the feature is
  // on, so the feature-off capture and its caches stay exactly as before.
  const bool nested_dielectrics = [&] {
    if (auto value = item(data, "rendering", false))
      if (auto flag = item(value, "nested_dielectrics", false))
        return flag.is(py::bool_(true));
    return committed_state && committed_state->rendering && committed_state->rendering->nested_dielectrics;
  }();
  const auto requested_fields = [nested_dielectrics](const ch::MaterialData &material, bool displacement_only = false) {
    return material_geometry_fields(material, displacement_only, nested_dielectrics);
  };
  const auto rendering =
      item(data, "rendering", false)
          ? std::optional<ch::RenderSettings>(render_settings(item(data, "rendering"), h.config.samples))
          : std::nullopt;
  if (rendering)
    ch::validate_render_settings(*rendering); // Reject before cooking, capture or queued submission.
  std::optional<std::vector<std::string>> requested_passes;
  const auto requested_aovs = item(data, "aovs", false)
                                  ? std::optional<std::vector<ch::AovDefinition>>(aov_definitions(item(data, "aovs")))
                                  : std::nullopt;
  std::set<std::string> active_aovs;
  for (const auto &aov : requested_aovs                ? *requested_aovs
                         : h.desired &&h.desired->aovs ? *h.desired->aovs
                                                       : h.config.aovs)
    active_aovs.insert(aov.name);
  const auto requested_pointiness = [&](const ch::MaterialData &material, bool displacement_only = false) {
    return material_geometry_pointiness(material, displacement_only, active_aovs);
  };
  for (auto &request : requests) {
    const ch::MaterialData *source = nullptr;
    for (const auto &m : job.update.materials)
      if (m.key == request.material_key)
        source = &m;
    if (!source && committed_state && committed_state->materials.contains(request.material_key))
      source = &committed_state->materials.at(request.material_key);
    if (source) {
      request.shader_attributes = requested_fields(*source);
      request.pointiness = requested_pointiness(*source);
    }
  }
  const auto requested_groups =
      item(data, "lightgroups", false)
          ? std::optional<std::vector<std::string>>(lightgroup_definitions(item(data, "lightgroups")))
          : std::nullopt;
  const auto requested_crypto =
      item(data, "cryptomatte", false)
          ? std::optional<ch::CryptomatteSettings>(cryptomatte_settings(item(data, "cryptomatte")))
          : std::nullopt;
  if (auto value = item(data, "passes", false)) {
    requested_passes = read_pass_names(value);
  }
  if (requested_aovs || requested_groups) {
    const auto &aovs = requested_aovs                 ? *requested_aovs
                       : h.desired && h.desired->aovs ? *h.desired->aovs
                                                      : h.config.aovs;
    const auto &groups = requested_groups                      ? *requested_groups
                         : h.desired && h.desired->lightgroups ? *h.desired->lightgroups
                                                               : h.config.lightgroups;
    ch::validate_pass_outputs(aovs, groups);
  }
  // Pure parameter validation never changes capture caches. Only an actual
  // authoritative geometry capture needs invalidation after a failed commit.
  job.select_native = bool(item(data, "geometry", false)) || bool(item(data, "material_resolver", false));
  auto resolver = item(data, "material_resolver", false);
  auto &prepared_graphs = job.graphs;
  // Temporal native bindings may visit the configured uniform fallback.
  // Its values are already owned; never ask the native resolver to find it.
  if (h.desired)
    for (const auto &[key, material] : h.desired->materials)
      if (!key.starts_with("native-material:"))
        prepared_graphs.emplace(key, material);
  for (const auto &material : job.update.materials)
    prepared_graphs.insert_or_assign(material.key, material);
  std::map<std::pair<std::string, std::size_t>, std::string> displacement_tokens;
  const ch::NativeMaterialPreparation prepare_graphs{
      [&](const ch::NativeMaterialLookup &candidates) {
        ch::prepare_native_material_graphs(prepared_graphs, candidates, [&](const auto &reference) {
          return prepare_material_graph(resolver, reference, time);
        });
        std::set<std::string> result;
        for (const auto &[path, reference] : candidates) {
          const auto names = requested_fields(prepared_graphs.at(reference.key), candidates.displacement_only);
          result.insert(names.begin(), names.end());
        }
        if (candidates.elements)
          for (const auto &reference : candidates.elements->references) {
            const auto names = requested_fields(prepared_graphs.at(reference->key), candidates.displacement_only);
            result.insert(names.begin(), names.end());
          }
        return result;
      },
      [&](const std::string &key) {
        const auto &material = prepared_graphs.at(key);
        return material.graph && material.graph->displacement &&
               material.graph->displacement_method != ch::DisplacementMethod::Bump;
      },
      [&](const std::string &nominal, const std::string &sampled, std::size_t step) {
        const auto &a = prepared_graphs.at(nominal);
        const auto &b = prepared_graphs.at(sampled);
        const auto graph = [&](const ch::MaterialData &material) -> const ch::ShaderGraphData * {
          if (!material.graph || !material.graph->displacement ||
              material.graph->displacement_method == ch::DisplacementMethod::Bump)
            return nullptr;
          return material.displacement_samples.empty() ? &*material.graph : &material.displacement_samples.at(step);
        };
        const auto *first = graph(a);
        const auto *second = graph(b);
        if (!first)
          return true;
        return second && ch::displacement_snapshot(*first) == ch::displacement_snapshot(*second);
      },
      [&](const std::string &key, std::size_t step) {
        const auto identity = std::make_pair(key, step);
        if (const auto found = displacement_tokens.find(identity); found != displacement_tokens.end())
          return found->second;
        const auto &source = prepared_graphs.at(key);
        ch::MaterialData material;
        material.kind = ch::MaterialKind::Graph;
        material.graph = ch::displacement_snapshot(
            source.displacement_samples.empty() ? *source.graph : source.displacement_samples.at(step));
        // Reuse the deterministic owned-value encoding. No engine scene or
        // transport is touched; exact graph equality still confirms collisions.
        ch::WorkerUpdate value;
        value.scene.materials.push_back(std::move(material));
        const auto bytes = ch::encode_worker_update(value);
        const auto token = XXH3_128bits(bytes.data(), bytes.size());
        auto text = std::to_string(token.low64) + ":" + std::to_string(token.high64);
        displacement_tokens.emplace(identity, text);
        return text;
      },
      [&](const std::string &key, bool displacement_only) {
        return requested_pointiness(prepared_graphs.at(key), displacement_only);
      },
      [&](const std::string &key) { return prepared_graphs.at(key).source_diagnostics; }};
  if (!item(data, "geometry", false) && resolver) {
    // A material value edit checked by prepare_material_edit: its graphs are already resolved and every
    // other native graph stays as committed, so the loop below resolves nothing.
    // Without a matching check (another apply came between) every reference is resolved again.
    if (const auto edit = item(data, "material_edit", false); edit && truth(edit) && h.material_edit &&
                                                              h.material_edit->base == committed_state &&
                                                              h.material_edit->time == time) {
      for (const auto &[key, material] : committed_state->materials)
        if (key.starts_with("native-material:"))
          prepared_graphs.insert_or_assign(key, material);
      for (const auto &[key, material] : h.material_edit->graphs)
        prepared_graphs.insert_or_assign(key, material);
    }
    h.material_edit.reset();
    // Two temporal variants can have the same requested-frame assignment.
    // A map keyed only by that assignment must not drop the second recipe.
    for (const auto &reference : h.adapter.material_references()) {
      ch::NativeMaterialLookup candidate;
      candidate.emplace(reference.assignment(), reference);
      prepare_graphs(candidate);
    }
  }
  job.touched_adapter = bool(item(data, "geometry", false)) || bool(item(data, "deleted_objects", false)) ||
                        bool(item(data, "deleted_geometry", false));
  job.geometry = h.adapter.prepare(requests, time, bool(item(data, "geometry", false)), motion, prepare_graphs,
                                   job.roots, job.roots ? &job.preparation : nullptr);
  auto &update = job.update;
  if (auto issues = item(data, "source_diagnostics", false))
    update.source_diagnostics = source_issues(issues);
  update.rendering = rendering;
  update.passes = std::move(requested_passes);
  update.aovs = requested_aovs;
  update.lightgroups = requested_groups;
  update.cryptomatte = requested_crypto;
  if (auto settings = item(data, "denoising", false))
    update.denoising = denoise_settings(settings);
  if (motion_data || item(data, "geometry", false))
    update.motion_blur = window.enabled && !motion.vectors;
  if (auto v = item(data, "motion_blur", false))
    update.motion_blur = int(truth(v)) > 0;
  if (auto v = item(data, "defer_camera_subdivision", false)) {
    if (!py::isinstance<py::bool_>(v))
      throw std::invalid_argument("defer_camera_subdivision requires a boolean");
    update.defer_camera_subdivision = v.is(py::bool_(true));
  }
  prepare_camera_update(update, data, window, h.width, h.height);
  if (auto value = item(data, "background", false))
    update.background = vector3(value);
  if (auto value = item(data, "world", false)) {
    ch::WorldData world;
    if (auto source = item(value, "source_path", false))
      world.source_path = string(source);
    if (auto graph = item(value, "graph", false); graph && !graph.is_none())
      world.graph = shader_graph(graph);
    if (world.graph && !world.graph->image_time &&
        std::any_of(world.graph->nodes.begin(), world.graph->nodes.end(), [](const auto &node) {
          const auto found = node.parameters.find("filename");
          return found != node.parameters.end() && std::holds_alternative<std::string>(found->second) &&
                 std::get<std::string>(found->second).starts_with("op:");
        }))
      world.graph->image_time = time;
    if (auto p = item(value, "visibility", false))
      world.visibility = unsigned(world_integer(p));
    for (const auto *name : {"use_mis", "cast_shadow", "use_caustics"})
      if (auto p = item(value, name, false)) {
        if (!py::isinstance<py::bool_>(p))
          throw std::invalid_argument(std::string("World ") + name + " requires a boolean");
        if (std::string(name) == "use_mis")
          world.use_mis = p.is(py::bool_(true));
        else if (std::string(name) == "cast_shadow")
          world.cast_shadow = p.is(py::bool_(true));
        else
          world.use_caustics = p.is(py::bool_(true));
      }
    if (auto p = item(value, "map_resolution", false))
      world.map_resolution = world_integer(p);
    if (auto p = item(value, "max_bounces", false))
      world.max_bounces = world_integer(p);
    if (auto p = item(value, "volume_sampling", false)) {
      const auto name = string(p);
      if (name == "distance")
        world.volume_sampling = 0;
      else if (name == "equiangular")
        world.volume_sampling = 1;
      else if (name == "multiple_importance")
        world.volume_sampling = 2;
      else
        throw std::invalid_argument("World volume sampling must be distance, equiangular or multiple_importance");
    }
    update.world = std::move(world);
  }
  if (auto value = item(data, "background_lightgroup", false))
    update.background_lightgroup = shader_string(value);
  if (auto value = item(data, "background_shadow_catcher", false)) {
    if (!py::isinstance<py::bool_>(value))
      throw std::invalid_argument("background_shadow_catcher requires a boolean");
    update.background_shadow_catcher = value.is(py::bool_(true));
  }
  for (const auto *key : {"transparent_background", "transparent_glass"})
    if (auto value = item(data, key, false)) {
      if (!py::isinstance<py::bool_>(value))
        throw std::invalid_argument(std::string(key) + " requires a boolean");
      if (std::string(key) == "transparent_background")
        update.transparent_background = value.is(py::bool_(true));
      else
        update.transparent_glass = value.is(py::bool_(true));
    }
  if (auto value = item(data, "transparent_roughness_threshold", false))
    update.transparent_roughness_threshold = float(number(value));
  if (auto value = item(data, "crop", false)) {
    auto values = sequence(value);
    if (values.size() != 4)
      throw std::invalid_argument("Crop requires x,y,width,height");
    update.crop =
        ch::Crop{int(number(values[0])), int(number(values[1])), int(number(values[2])), int(number(values[3]))};
  }
  if (auto list = item(data, "lights", false))
    for (auto value : sequence(list)) {
      ch::LightData l;
      l.key = string(item(value, "key"));
      l.strength = vector3(item(value, "strength"));
      if (auto v = item(value, "transform", false))
        l.transform = matrix(v);
      if (auto v = item(value, "source_path", false))
        l.source_path = shader_string(v);
      if (auto v = item(value, "linking_id", false))
        l.linking_id = string(v);
      if (auto v = item(value, "graph", false); v && !v.is_none())
        l.graph = shader_graph(v);
      if (l.graph && !l.graph->image_time &&
          std::any_of(l.graph->nodes.begin(), l.graph->nodes.end(), [](const auto &node) {
            const auto file = node.parameters.find("filename");
            return file != node.parameters.end() && std::holds_alternative<std::string>(file->second) &&
                   std::get<std::string>(file->second).starts_with("op:");
          }))
        l.graph->image_time = time;
      if (auto v = item(value, "lightgroup", false))
        l.lightgroup = shader_string(v);
      if (auto v = item(value, "cryptomatte_object", false))
        l.cryptomatte_object = shader_string(v);
      if (auto v = item(value, "cryptomatte_asset", false))
        l.cryptomatte_asset = shader_string(v);
      ch::validate_cryptomatte_name(l.cryptomatte_object);
      ch::validate_cryptomatte_name(l.cryptomatte_asset);
      if (auto v = item(value, "radius", false))
        l.radius = float(number(v));
      if (auto v = item(value, "size", false)) {
        auto values = sequence(v);
        if (values.size() != 2)
          throw std::invalid_argument("Area size requires two values");
        l.size_u = float(number(values[0]));
        l.size_v = float(number(values[1]));
      }
      if (auto v = item(value, "angle", false))
        l.angle = float(number(v));
      if (auto v = item(value, "spread", false))
        l.spread = float(number(v));
      if (auto v = item(value, "spot_smooth", false))
        l.spot_smooth = float(number(v));
      if (auto v = item(value, "cone", false))
        l.cone = ch::LightCone{float(number(item(v, "inner_half_angle"))), float(number(item(v, "outer_half_angle"))),
                               float(number(item(v, "rolloff")))};
      if (auto v = item(value, "cone", false))
        if (auto reverse = item(v, "reverse_axis", false)) {
          if (!py::isinstance<py::bool_>(reverse))
            throw std::invalid_argument("Cone reverse_axis requires a boolean");
          l.cone->reverse_axis = reverse.is(py::bool_(true));
        }
      if (auto v = item(value, "max_bounces", false)) {
        if (!py::isinstance<py::int_>(v) || py::isinstance<py::bool_>(v))
          throw std::invalid_argument("Light max_bounces requires an integer");
        const auto count = integer<long long>(v, "Light max_bounces requires 0–1024");
        if (count < 0 || count > 1024)
          throw std::invalid_argument("Light max_bounces requires 0–1024");
        l.max_bounces = int(count);
      }
      for (auto [name, destination] :
           {std::pair{"normalize", &l.normalize}, std::pair{"cast_shadow", &l.cast_shadow},
            std::pair{"use_mis", &l.use_mis}, std::pair{"ellipse", &l.ellipse}, std::pair{"is_sphere", &l.is_sphere},
            std::pair{"use_caustics", &l.use_caustics}, std::pair{"shadow_catcher", &l.shadow_catcher},
            std::pair{"is_portal", &l.is_portal}})
        if (auto v = item(value, name, false)) {
          if (!py::isinstance<py::bool_>(v))
            throw std::invalid_argument(std::string(name) + " requires a boolean");
          *destination = v.is(py::bool_(true));
        }
      if (auto v = item(value, "visibility", false)) {
        const auto mask = integer<unsigned long>(v, "Light ray visibility requires an unsigned 8-bit mask");
        if (mask > 255)
          throw std::invalid_argument("Light ray visibility requires an unsigned 8-bit mask");
        l.visibility = static_cast<std::uint32_t>(mask);
      }
      if (auto v = item(value, "kind", false)) {
        const auto k = string(v);
        if (k == "area")
          l.kind = ch::LightKind::Area;
        else if (k == "sun")
          l.kind = ch::LightKind::Sun;
        else if (k == "spot")
          l.kind = ch::LightKind::Spot;
        else if (k != "point")
          throw std::invalid_argument("Unknown light kind: " + k);
      }
      update.lights.push_back(std::move(l));
    }
  if (auto settings = item(data, "linking", false)) {
    ch::LinkingSettings linking;
    for (auto [name, target] : {std::pair{"receivers", &linking.receivers}, std::pair{"blockers", &linking.blockers}})
      if (auto values = item(settings, name, false))
        for (auto value : sequence(values)) {
          ch::LinkingRule rule;
          for (auto object : sequence(item(value, "objects")))
            rule.objects.push_back(string(object));
          if (auto emitters = item(value, "emitters", false))
            for (auto emitter : sequence(emitters))
              rule.emitters.push_back(string(emitter));
          if (auto exclude = item(value, "exclude", false)) {
            if (!py::isinstance<py::bool_>(exclude))
              throw std::invalid_argument("Linking exclude requires a boolean");
            rule.exclude = exclude.is(py::bool_(true));
          }
          target->push_back(std::move(rule));
        }
    update.linking = std::move(linking);
  }
  for (auto [key, destination] :
       {std::pair{"deleted_objects", &update.deleted_objects}, std::pair{"deleted_geometry", &update.deleted_geometry},
        std::pair{"deleted_materials", &update.deleted_materials}, std::pair{"deleted_lights", &update.deleted_lights}})
    if (auto list = item(data, key, false))
      for (auto value : sequence(list)) {
        const auto name = string(value);
        if (destination == &update.deleted_materials && name.starts_with("native-material:"))
          throw std::invalid_argument("Native material retirement is owned by the assignment resolver");
        destination->push_back(name);
      }
}

void convert_apply_data(ApplyJob &job) {
  int expected = 0;
  if (!job.phase.compare_exchange_strong(expected, 1))
    throw std::logic_error("Scene conversion can run only once");
  const auto started = std::chrono::steady_clock::now();
  try {
    auto &h = *job.handle;
    auto &update = job.update;
    const auto *profile_value = std::getenv("CYCLES_HOUDINI_PROFILE_PACKED_ASSEMBLY");
    const bool profiling = profile_value && std::strcmp(profile_value, "1") == 0;
    auto phase_started = std::chrono::steady_clock::now();
    const auto phase = [&](const char *name) {
      if (profiling) {
        const auto now = std::chrono::steady_clock::now();
        h.adapter.record_profile_phase(name, std::chrono::duration<double>(now - phase_started).count());
        phase_started = now;
      }
    };
    auto geometry = h.adapter.convert(job.geometry);
    phase("apply_convert");
    h.retired_snapshot.reset();
    phase("apply_retired_snapshot");
    if (geometry.source_diagnostics) {
      if (!update.source_diagnostics)
        update.source_diagnostics.emplace();
      update.source_diagnostics->insert(update.source_diagnostics->end(), geometry.source_diagnostics->begin(),
                                        geometry.source_diagnostics->end());
    }
    update.meshes = std::move(geometry.meshes);
    update.points = std::move(geometry.points);
    update.curves = std::move(geometry.curves);
    update.volumes = std::move(geometry.volumes);
    update.objects = std::move(geometry.objects);
    update.object_poses = std::move(geometry.object_poses);
    update.object_attributes = std::move(geometry.object_attributes);
    update.deleted_objects.insert(update.deleted_objects.end(), geometry.deleted_objects.begin(),
                                  geometry.deleted_objects.end());
    update.deleted_geometry.insert(update.deleted_geometry.end(), geometry.deleted_geometry.begin(),
                                   geometry.deleted_geometry.end());
    phase("apply_move");
    h.adapter.expand_deletions(update);
    h.adapter.prepare_commit(update);
    phase("apply_prepare_commit");
    job.references = h.material_references;
    job.native_keys = h.native_material_keys;
    if (job.select_native) {
      job.references = h.adapter.material_references();
      job.native_keys.clear();
      for (const auto &reference : job.references)
        job.native_keys.insert(reference.key);
      for (auto &graph : ch::select_native_material_graphs(job.graphs, job.references))
        update.materials.push_back(std::move(graph));
      for (const auto &key : h.native_material_keys)
        if (!job.native_keys.contains(key))
          update.deleted_materials.push_back(key);
    }
    phase("apply_materials");
    if (h.capture_only) {
      // The snapshot shares the adapter's prepared object table when that is
      // provably the table the update would build (diagnostic switch:
      // CYCLES_HOUDINI_COPY_SNAPSHOT_OBJECTS=1 always builds a separate copy).
      const auto *copy = std::getenv("CYCLES_HOUDINI_COPY_SNAPSHOT_OBJECTS");
      auto objects = copy && std::strcmp(copy, "1") == 0
                         ? std::shared_ptr<const ch::SceneSnapshotState::ObjectTable>{}
                         : h.adapter.snapshot_objects(h.desired ? h.desired->objects : nullptr, update);
      job.state = ch::snapshot_state(h.desired, update, std::move(objects));
      job.navigation = h.adapter.navigation();
    }
    phase("apply_snapshot_state");
    if (h.capture_only) {
      // The immutable snapshot owns these resources now. Image policy
      // acquisition only consumes shaders/lights/world. Release large
      // geometry/object DTOs on the converter before host publication.
      const auto release = [](auto &values) { std::remove_reference_t<decltype(values)>{}.swap(values); };
      release(update.objects);
      release(update.object_poses);
      release(update.object_attributes);
      release(update.meshes);
      release(update.points);
      release(update.curves);
      release(update.volumes);
      release(update.deleted_objects);
      release(update.deleted_geometry);
    }
    phase("apply_release");
    job.geometry.reset();
    job.roots.reset();
    job.graphs.clear();
    phase("apply_reset");
    job.conversion_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();
    job.phase = 2;
  } catch (...) {
    job.phase = 3;
    throw;
  }
}

bool acquire_apply_images(ApplyJob &job) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Scene image acquisition requires Houdini's host thread");
  if (job.phase != 2)
    throw std::logic_error("Scene geometry has not completed conversion");
  job.image_state = ch::shader_resource_state(job.handle->texture_state, job.update);
  job.images = job.handle->cop_images.acquire(ch::texture_image_requests(*job.image_state));
  job.phase = job.images->pending ? 8 : 10;
  return job.images->pending;
}

void convert_apply_images(ApplyJob &job) {
  int expected = 8;
  if (!job.phase.compare_exchange_strong(expected, 9))
    throw std::logic_error("Scene image conversion can run only once");
  try {
    ch::NativeCopImageResolver::convert(*job.images);
    job.phase = 10;
  } catch (...) {
    job.phase = 3;
    throw;
  }
}

py::object finish_apply_data(ApplyJob &job) {
  // Synchronous callers share the same resource contract; interactive capture
  // explicitly schedules the owned pixel conversion on its conversion thread.
  if (job.phase == 2 && acquire_apply_images(job))
    convert_apply_images(job);
  if (job.phase != 10 || job.finished)
    throw std::logic_error("Scene conversion is not ready or was already consumed");
  auto &h = *job.handle;
  auto &update = job.update;
  auto issues = update.source_diagnostics.value_or(h.captured_source_diagnostics);
  std::erase_if(issues, [](const auto &issue) {
    return issue.code == "world.motion_ignored" && issue.source.starts_with("op:");
  });
  if (job.image_state->motion_blur.value_or(false) && job.image_state->world && job.image_state->world->graph) {
    const auto &graph = *job.image_state->world->graph;
    const bool reported = std::any_of(issues.begin(), issues.end(),
                                      [](const auto &issue) { return issue.code == "world.motion_ignored"; });
    if (!reported)
      for (const auto &node : graph.nodes) {
        if (node.definition != "cycles/image_texture/1" && node.definition != "cycles/environment_texture/1")
          continue;
        const auto file = node.parameters.find("filename");
        if (file == node.parameters.end())
          continue;
        const auto *path = std::get_if<std::string>(&file->second);
        if (!path || !path->starts_with("op:"))
          continue;
        const auto image = job.images->entries.find({*path, graph.image_time.value_or(0.)});
        if (image != job.images->entries.end() && image->second.image && image->second.image->error.empty() &&
            image->second.time_dependent) {
          issues.push_back(
              {"world.motion_ignored", *path, "world",
               "Animated COP world images use requested-frame sampling; temporal world sampling is not supported",
               "Rendering world lighting at the requested frame without world motion blur"});
          break;
        }
      }
  }
  const auto prior = update.source_diagnostics.value_or(h.captured_source_diagnostics);
  if (issues != prior) {
    update.source_diagnostics = std::move(issues);
    if (h.capture_only) {
      ch::SceneUpdate diagnostics;
      diagnostics.source_diagnostics = update.source_diagnostics;
      job.state = ch::snapshot_state(job.state, diagnostics);
    }
  }
  if (job.color->policy().render_policy_id != ch::current_color_configuration()->policy().render_policy_id)
    throw ch::SceneUpdateRejected("Captured working color policy was superseded; recapture scene");
  py::object result;
  if (h.capture_only) {
    auto owned = std::make_shared<ch::OwnedSceneSnapshot>();
    owned->state = job.state;
    owned->native_navigation = job.navigation;
    // Host-only policy/header acquisition stays on the Houdini thread.
    // It uses cached observations for unchanged graphs/files.
    {
      ReleaseGIL release;
      auto textures = std::make_shared<ch::OwnedTexturePolicy>(h.texture_resolver->capture(*owned->state));
      textures->images = job.images->images;
      owned->textures = std::move(textures);
      owned->color = job.color;
      owned->portable_color = ch::snapshot_color_policy(owned->color);
    }
    owned->reuse_if_unchanged = job.reuse;
    if (h.retired_snapshot)
      throw std::logic_error("Prior capture snapshot was not retired before publication");
    auto pointer = std::make_unique<SnapshotPtr>(std::move(owned));
    result = py::capsule(pointer.get(), "cycles_houdini.SceneSnapshot", destroy_owned<SnapshotPtr>);
    if (h.desired != (*pointer)->state)
      h.retired_snapshot = std::move(h.desired);
    h.desired = (*pointer)->state;
    pointer.release();
  } else {
    std::uint64_t generation;
    auto images = std::make_shared<ch::OwnedTexturePolicy>();
    images->images = job.images->images;
    const auto before = h.policy_slot->replace(std::move(images));
    try {
      ReleaseGIL release;
      generation = h.session->apply(update, job.reuse);
    } catch (...) {
      h.policy_slot->replace(before);
      throw;
    }
    result = py::int_(generation);
  }
  if (!h.capture_only) {
    if (update.aovs)
      h.config.aovs = *update.aovs;
    if (update.lightgroups)
      h.config.lightgroups = *update.lightgroups;
    if (update.cryptomatte)
      h.config.cryptomatte = *update.cryptomatte;
  }
  h.adapter.commit();
  h.accepted_material_paths = h.adapter.material_paths();
  h.accepted_stylesheet_resources = h.adapter.stylesheet_resources();
  h.native_material_keys = std::move(job.native_keys);
  h.texture_state = job.image_state;
  h.cop_images.commit(job.images);
  if (update.source_diagnostics)
    h.captured_source_diagnostics = *update.source_diagnostics;
  h.material_references = std::move(job.references);
  h.published_stats = capture_stats(h);
  job.finished = true;
  return result;
}

void destroy_apply_job(void *pointer) {
  auto *job = static_cast<ApplyJob *>(pointer);
  auto owner = std::move(job->owner);
  if (!job->released) {
    if (job->touched_adapter && !job->finished)
      job->handle->adapter.invalidate();
    job->handle->end_apply();
  }
  {
    ReleaseGIL release;
    delete job;
  }
}

ApplyJob &apply_job(py::handle capsule) {
  auto *job = capsule_pointer<ApplyJob>(capsule, "cycles_houdini.ApplyJob");
  if (!job)
    throw std::invalid_argument("Invalid scene conversion job");
  return *job;
}

py::object acquire_apply(py::handle capsule_arg, py::handle data_arg, py::handle time_arg) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Scene acquisition requires Houdini's host thread");
  py::handle capsule, data;
  double time;
  capsule = capsule_arg;
  data = data_arg;
  time = argument_real<double>(time_arg);
  auto &h = handle(capsule);
  if (!h.capture_only)
    throw std::logic_error("Capture handle is unavailable for acquisition");
  auto job = std::make_unique<ApplyJob>();
  h.begin_apply();
  job->handle = &h;
  job->phase = 5;
  try {
    job->color = ch::current_color_configuration();
    job->roots =
        ch::acquire_scene_roots(geometry_requests(item(data, "geometry", false)), time, &h.adapter.point_instances());
  } catch (...) {
    h.end_apply();
    throw;
  }
  try {
    py::capsule result(job.get(), "cycles_houdini.ApplyJob", destroy_apply_job);
    job->owner = py::reinterpret_borrow<py::object>(capsule);
    job.release();
    return result;
  } catch (...) {
    if (job && job->touched_adapter)
      h.adapter.invalidate();
    h.end_apply();
    throw;
  }
}

py::object prewarm_apply(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  auto &job = apply_job(capsule);
  int expected = 5;
  if (!job.phase.compare_exchange_strong(expected, 6))
    throw std::logic_error("Scene scan can run only once");
  const auto started = std::chrono::steady_clock::now();
  try {
    ReleaseGIL release;
    job.handle->retired_snapshot.reset();
    job.handle->adapter.prewarm_roots(*job.roots);
  } catch (...) {
    job.phase = 3;
    throw;
  }
  job.phase = 7;
  return py::float_(std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count());
}

py::object prepare_apply(py::handle capsule_arg, py::handle data_arg, py::handle time_arg, py::handle reuse_arg,
                         py::handle warm_arg) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Scene acquisition requires Houdini's host thread");
  py::handle capsule, data, warm = nullptr;
  double time;
  int reuse = 0;
  capsule = capsule_arg;
  data = data_arg;
  time = argument_real<double>(time_arg);
  reuse = truth(reuse_arg);
  if (!is_omitted(warm_arg))
    warm = warm_arg;
  auto &h = handle(capsule);
  if (!h.capture_only)
    throw std::logic_error("Asynchronous capture requires a capture-only session");
  if (h.capture_retired)
    throw std::logic_error("Native capture resources were retired");
  if (warm) {
    auto &job = apply_job(warm);
    if (job.handle != &h || job.phase != 7 || job.released)
      throw std::logic_error("Invalid prewarmed capture");
    job.preparation.begin_turn();
    try {
      prepare_apply_data(job, h, data, time, reuse != 0);
      job.phase = 0;
    } catch (const ch::NativePreparationPending &) {
      ++job.preparation.turns;
      return py::bool_(false);
    } catch (...) {
      job.phase = 3;
      throw;
    }
    ++job.preparation.turns;
    return py::bool_(true);
  }
  auto job = std::make_unique<ApplyJob>();
  h.begin_apply();
  try {
    prepare_apply_data(*job, h, data, time, reuse != 0);
  } catch (...) {
    if (job->touched_adapter)
      h.adapter.invalidate();
    h.end_apply();
    throw;
  }
  try {
    py::capsule result(job.get(), "cycles_houdini.ApplyJob", destroy_apply_job);
    job->owner = py::reinterpret_borrow<py::object>(capsule);
    job.release();
    return result;
  } catch (...) {
    if (job && job->touched_adapter)
      h.adapter.invalidate();
    h.end_apply();
    throw;
  }
}

py::object expand_apply(py::handle capsule_arg) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Packed acquisition requires Houdini's host thread");
  py::handle capsule;
  capsule = capsule_arg;
  auto &job = apply_job(capsule);
  if (job.phase != 7 || job.released)
    throw std::logic_error("Packed frontier has not been scanned");
  bool more;
  try {
    more = job.handle->adapter.acquire_packed_roots(*job.roots);
  } catch (...) {
    job.phase = 3;
    throw;
  }
  if (more)
    job.phase = 5;
  return py::bool_(more);
}

py::object convert_apply(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  auto &job = apply_job(capsule);
  {
    ReleaseGIL release;
    convert_apply_data(job);
  }
  return py::float_(job.conversion_seconds);
}

py::object acquire_images(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  return py::bool_(acquire_apply_images(apply_job(capsule)));
}

py::object convert_images(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  auto &job = apply_job(capsule);
  {
    ReleaseGIL release;
    convert_apply_images(job);
  }
  return py::float_(job.images->conversion_seconds);
}

py::object finish_apply(py::handle capsule_arg) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Scene completion requires Houdini's host thread");
  py::handle capsule;
  capsule = capsule_arg;
  auto &job = apply_job(capsule);
  auto result = finish_apply_data(job);
  if (result) {
    job.handle->end_apply();
    job.released = true;
  }
  return result;
}

py::object discard_apply(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  auto &job = apply_job(capsule);
  auto phase = job.phase.load();
  do {
    if (phase == 1 || phase == 6 || phase == 9)
      throw std::logic_error("Cannot discard a running scene conversion");
  } while (!job.phase.compare_exchange_weak(phase, 4));
  const auto started = std::chrono::steady_clock::now();
  const auto objects = job.state ? job.state->objects->size() : 0;
  {
    ReleaseGIL release;
    // Classification caches contain immutable metadata. Keep applying true
    // until both invalidation and the job's owned payload have retired.
    if (!job.released && job.touched_adapter)
      job.handle->adapter.invalidate();
    release_apply_payload(job);
  }
  if (!job.released) {
    job.handle->end_apply();
    job.released = true;
  }
  return py::dict(py::arg("seconds") =
                      py::float_(std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count()),
                  py::arg("on_host_thread") = py::bool_(UT_Thread::isMainThread()),
                  py::arg("snapshot_objects") = py::int_(static_cast<unsigned long long>(objects)));
}

py::object apply(py::handle capsule_arg, py::handle data_arg, py::handle time_arg, py::handle reuse_arg) {
  py::handle capsule, data;
  double time;
  int reuse = 0;
  capsule = capsule_arg;
  data = data_arg;
  time = argument_real<double>(time_arg);
  reuse = truth(reuse_arg);
  auto &h = handle(capsule);
  ApplyGuard guard{h};
  ApplyJob job;
  try {
    prepare_apply_data(job, h, data, time, reuse != 0);
    {
      ReleaseGIL release;
      convert_apply_data(job);
    }
    auto result = finish_apply_data(job);
    if (!result && job.touched_adapter && !job.finished)
      h.adapter.invalidate();
    return result;
  } catch (...) {
    if (job.touched_adapter && !job.finished)
      h.adapter.invalidate();
    throw;
  }
}

void motion_warning_metadata(py::dict result, const ch::ExtractionStats &stats) {
  for (const auto &[name, value] : {std::pair{"material_motion_cooks", stats.material_motion_cooks},
                                    std::pair{"material_motion_samples", stats.material_motion_samples},
                                    std::pair{"material_motion_cache_hits", stats.material_motion_cache_hits}}) {
    py::int_ number = py::int_(value);
    result[name] = number;
  }
  py::list warnings = py::list(0);
  for (const auto &warning : stats.motion_warnings) {
    py::str value = py::str(warning.c_str());
    warnings.append(value);
  }
  result["motion_warnings"] = warnings;
}

// Internal ownership experiment. This does not select an artist execution mode.
std::atomic<int> geometry_leases{};

py::object preserve_geometry(py::str path_arg, py::handle time_arg, py::handle copy_arg, py::handle material_arg) {
  const char *path, *material = nullptr;
  double time;
  int copy = 0;
  const auto path_storage = path_arg.cast<std::string>();
  path = c_string_checked(path_storage);
  time = argument_real<double>(time_arg);
  copy = truth(copy_arg);
  std::string material_storage;
  if (!is_omitted(material_arg) && !material_arg.is_none()) {
    if (!py::isinstance<py::str>(material_arg))
      throw py::type_error("material must be a string or None");
    material_storage = material_arg.cast<std::string>();
    material = c_string_checked(material_storage);
  }
  // Like apply(), callers acquire only in a supported host cook context.
  if (!UT_Thread::isMainThread())
    throw std::logic_error("SOP preservation requires the host thread");
  using Clock = std::chrono::steady_clock;
  const auto started = Clock::now();
  auto *sop = OPgetDirector()->findSOPNode(path);
  if (!sop)
    throw std::invalid_argument("Missing SOP for geometry preservation");
  OP_Context context(time);
  auto handle = sop->getCookedGeoHandle(context);
  if (!handle || sop->error() >= UT_ERROR_ABORT)
    throw std::runtime_error("SOP preservation cook failed");
  const auto cooked = Clock::now();
  const auto source_id = handle.gdp()->getUniqueId();
  const std::array<long long, 3> source_data_ids = {handle.gdp()->getP()->getDataId(),
                                                    handle.gdp()->getTopology().getDataId(),
                                                    handle.gdp()->getPrimitiveList().getDataId()};
  if (copy)
    handle = handle.duplicateGeometry(GA_DATA_ID_CLONE);
  auto lease = std::make_unique<GeometryLease>(std::move(handle), path);
  lease->request.sop_path = path;
  lease->request.geometry_key = "preserved";
  lease->request.material_key = "probe";
  lease->request.material_mode = ch::NativeMaterialMode::Uniform;
  if (material) {
    const auto &source = *lease->detail.get();
    const auto partitions = ch::partition_geometry(source);
    const auto *mesh = partitions.find(ch::NativeGeometryKind::Mesh);
    if (partitions.count != 1 || !mesh)
      throw std::invalid_argument("Native binding ownership probe requires only mesh geometry");
    lease->request.material_mode = ch::NativeMaterialMode::Native;
    lease->request.object_material_path = material;
    ch::NativeMaterialBindingCache bindings;
    lease->request.native_materials =
        ch::freeze_native_materials(bindings.inspect(source, mesh), lease->request, false);
  }
  lease->source_id = source_id;
  lease->source_data_ids = source_data_ids;
  lease->cook_seconds = std::chrono::duration<double>(cooked - started).count();
  lease->acquire_seconds = std::chrono::duration<double>(Clock::now() - cooked).count();
  auto result = py::capsule(lease.get(), "cycles_houdini.GeometryLease", destroy_owned<GeometryLease>);
  if (result)
    lease.release();
  return result;
}

py::object convert_preserved_mesh(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  const auto *lease = capsule_pointer<GeometryLease>(capsule, "cycles_houdini.GeometryLease");
  if (!lease)
    throw std::invalid_argument("Invalid geometry lease");
  const auto &request = lease->request;
  std::string fingerprint;
  std::uint64_t points{}, triangles{}, memory{}, detail_id{};
  double seconds{};
  std::vector<std::string> material_keys;
  std::vector<std::uint64_t> material_counts;
  std::array<double, 3> sums{};
  std::array<double, 3> first{};
  std::array<long long, 3> identifiers{};
  {
    ReleaseGIL release;
    const auto started = std::chrono::steady_clock::now();
    const auto &source = *lease->detail.get();
    // Actual production conversion with frozen material policy: no OP
    // lookup, Python callback or render session is accessed by this reader.
    const auto data = ch::capture_mesh(source, request, 1);
    auto *hash = XXH3_createState();
    if (!hash)
      throw std::bad_alloc();
    struct Cleanup {
      XXH3_state_t *hash;
      ~Cleanup() { XXH3_freeState(hash); }
    } cleanup{hash};
    XXH3_128bits_reset(hash);
    const auto append = [&](const auto &values) {
      XXH3_128bits_update(hash, values.data(), values.size() * sizeof(values[0]));
    };
    append(data.positions);
    append(data.triangles);
    append(data.corner_normals);
    append(data.corner_uv);
    append(data.corner_colors);
    append(data.triangle_material);
    material_keys = data.materials;
    material_counts.resize(material_keys.size());
    for (const auto &key : material_keys) {
      const auto length = key.size();
      XXH3_128bits_update(hash, &length, sizeof(length));
      XXH3_128bits_update(hash, key.data(), key.size());
    }
    for (std::size_t i = 0; i < data.triangles.size(); ++i)
      ++material_counts.at(data.triangle_material.empty() ? 0 : data.triangle_material.at(i));
    const auto value = XXH3_128bits_digest(hash);
    fingerprint = std::to_string(value.high64) + ":" + std::to_string(value.low64);
    points = data.positions.size();
    triangles = data.triangles.size();
    memory = source.getMemoryUsage(true);
    detail_id = source.getUniqueId();
    identifiers = {source.getP()->getDataId(), source.getTopology().getDataId(), source.getPrimitiveList().getDataId()};
    for (const auto &p : data.positions) {
      sums[0] += p.x;
      sums[1] += p.y;
      sums[2] += p.z;
    }
    if (!data.positions.empty())
      first = {data.positions[0].x, data.positions[0].y, data.positions[0].z};
    seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();
  }
  py::list materials = py::list(0);
  for (std::size_t i = 0; i < material_keys.size(); ++i) {
    py::dict row = py::dict(py::arg("key") = py::str(material_keys[i].c_str()),
                            py::arg("triangles") = py::int_(material_counts[i]));
    materials.append(row);
  }
  return py::dict(
      py::arg("fingerprint") = py::str(fingerprint.c_str()), py::arg("points") = py::int_(points),
      py::arg("triangles") = py::int_(triangles), py::arg("source_memory_bytes") = py::int_(memory),
      py::arg("detail_id") = py::int_(detail_id), py::arg("source_id") = py::int_(lease->source_id),
      py::arg("data_ids") =
          py::make_tuple(py::int_(identifiers[0]), py::int_(identifiers[1]), py::int_(identifiers[2])),
      py::arg("source_data_ids") =
          py::make_tuple(py::int_(lease->source_data_ids[0]), py::int_(lease->source_data_ids[1]),
                         py::int_(lease->source_data_ids[2])),
      py::arg("conversion_seconds") = py::float_(seconds), py::arg("cook_seconds") = py::float_(lease->cook_seconds),
      py::arg("acquire_seconds") = py::float_(lease->acquire_seconds),
      py::arg("position_sums") = py::make_tuple(py::float_(sums[0]), py::float_(sums[1]), py::float_(sums[2])),
      py::arg("first_position") = py::make_tuple(py::float_(first[0]), py::float_(first[1]), py::float_(first[2])),
      py::arg("materials") = materials);
}

// Internal owned-data observation: calls the production curve converter on a
// frozen detail, without evaluating an OP or mutating source geometry.
py::object convert_preserved_curves(py::handle capsule_arg, py::handle divisions_arg, py::handle velocity_arg,
                                    py::handle names_arg) {
  py::handle capsule, names = py::none();
  int divisions = 16, velocity = 0;
  capsule = capsule_arg;
  divisions = argument_integer<int>(divisions_arg);
  velocity = truth(velocity_arg);
  if (!is_omitted(names_arg))
    names = names_arg;
  const auto *lease = capsule_pointer<GeometryLease>(capsule, "cycles_houdini.GeometryLease");
  if (!lease || divisions < 1 || divisions > 256)
    throw std::invalid_argument("Invalid curve observation or Segments per Span");
  auto request = lease->request;
  request.curve_segments = divisions;
  if (!names.is_none())
    for (auto name : sequence(names))
      request.shader_attributes.insert(string(name));
  ch::CurveData data;
  std::vector<ch::Float3> velocities, accelerations;
  std::vector<ch::SourceDiagnostic> issues;
  std::uint64_t indices{};
  const auto parallel_before = ch::curve_conversion_counters.parallel.load();
  {
    ReleaseGIL release;
    const auto &source = *lease->detail.get();
    const auto partitions = ch::partition_geometry(source);
    const auto *curves = partitions.find(ch::NativeGeometryKind::Curves);
    if (!curves)
      throw std::invalid_argument("Curve observation requires native curve primitives");
    data = ch::capture_curves(source, request, 1, indices, velocity ? &velocities : nullptr, nullptr, true, nullptr,
                              curves, &issues, velocity ? &accelerations : nullptr);
  }
  py::dict result = py::dict();
  py::dict attributes = py::dict();
  const auto bytes = [](py::dict target, const char *name, const auto &values) {
    target[name] =
        py::bytes(reinterpret_cast<const char *>(values.data()), Py_ssize_t(values.size() * sizeof(values[0])));
  };
  bytes(result, "positions", data.positions);
  bytes(result, "radii", data.radii);
  bytes(result, "first_key", data.first_key);
  bytes(result, "colors", data.colors);
  bytes(result, "uv", data.uv);
  bytes(result, "velocities", velocities);
  bytes(result, "accelerations", accelerations); // Point accel, read beside v.
  // Equivalence tests (serial against parallel curve conversion) compare these too.
  bytes(result, "randoms", data.randoms);
  bytes(result, "material_slots", data.material_slots);
  py::list curve_materials = py::list();
  for (const auto &material : data.materials)
    curve_materials.append(py::str(material));
  result["materials"] = curve_materials;
  result["index_ids"] = py::int_(indices);
  result["parallel"] = py::bool_(ch::curve_conversion_counters.parallel.load() != parallel_before);
  for (const auto &field : data.attributes) {
    py::dict entry = py::dict(py::arg("components") = py::int_(field.components));
    bytes(entry, "values", field.values);
    attributes[field.name.c_str()] = entry;
  }
  result["attributes"] = attributes;
  source_diagnostic_metadata(result, issues);
  return result;
}

py::object preserve_deformation(py::str path_arg, py::handle time_arg, py::handle fps_arg, py::handle shutter_arg,
                                py::handle samples_arg, py::handle material_arg) {
  const char *path, *material = nullptr;
  double time, fps, shutter;
  int samples = 3;
  const auto path_storage = path_arg.cast<std::string>();
  path = c_string_checked(path_storage);
  time = argument_real<double>(time_arg);
  fps = argument_real<double>(fps_arg);
  shutter = argument_real<double>(shutter_arg);
  samples = argument_integer<int>(samples_arg);
  std::string material_storage;
  if (!is_omitted(material_arg) && !material_arg.is_none()) {
    if (!py::isinstance<py::str>(material_arg))
      throw py::type_error("material must be a string or None");
    material_storage = material_arg.cast<std::string>();
    material = c_string_checked(material_storage);
  }
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Deformation preparation requires the host thread");
  auto *sop = OPgetDirector()->findSOPNode(path);
  if (!sop)
    throw std::invalid_argument("Missing SOP for deformation preservation");
  const auto started = std::chrono::steady_clock::now();
  OP_Context context(time);
  const auto handle = sop->getCookedGeoHandle(context);
  if (!handle || sop->error() >= UT_ERROR_ABORT)
    throw std::runtime_error("Deformation preservation cook failed");
  std::map<ch::NativeGeometryKind, ch::GeometryRequest> requests;
  for (const auto &selection : ch::partition_geometry(*handle.gdp()).selections)
    if (selection.active) {
      auto &request = requests[selection.kind];
      request.sop_path = path;
      request.geometry_key = "deformation-probe";
      request.material_key = "probe";
      request.material_mode = material ? ch::NativeMaterialMode::Native : ch::NativeMaterialMode::Uniform;
      if (material)
        request.object_material_path = material;
    }
  ch::NativeMotionSettings motion;
  motion.enabled = true;
  motion.frames_per_second = fps;
  motion.shutter_frames = shutter;
  motion.samples = samples;
  auto lease = std::make_unique<DeformationLease>();
  const auto acquired = std::chrono::steady_clock::now();
  lease->prepared =
      ch::prepare_deformation_set(*sop, requests, ch::native_motion_window(time, motion), lease->preparation);
  lease->sample_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - acquired).count();
  lease->seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();
  auto result = py::capsule(lease.get(), "cycles_houdini.DeformationLease", destroy_owned<DeformationLease>);
  if (result)
    lease.release();
  return result;
}

py::object convert_preserved_deformation(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  const auto *lease = capsule_pointer<DeformationLease>(capsule, "cycles_houdini.DeformationLease");
  if (!lease)
    throw std::invalid_argument("Invalid deformation lease");
  struct Observation {
    std::string kind;
    std::uint64_t points{};
    std::vector<std::array<double, 3>> first, sums;
    std::vector<std::string> materials;
  };
  std::vector<Observation> observations;
  std::string fingerprint;
  ch::ExtractionStats stats;
  {
    ReleaseGIL release;
    const auto data = ch::convert_deformation_set(*lease->prepared, 1, stats);
    auto *hash = XXH3_createState();
    if (!hash)
      throw std::bad_alloc();
    struct Cleanup {
      XXH3_state_t *value;
      ~Cleanup() { XXH3_freeState(value); }
    } cleanup{hash};
    XXH3_128bits_reset(hash);
    const auto append = [&](const auto &values) {
      const auto size = values.size();
      XXH3_128bits_update(hash, &size, sizeof(size));
      XXH3_128bits_update(hash, values.data(), values.size() * sizeof(values[0]));
    };
    for (const auto &[kind, item] : data) {
      Observation row;
      row.kind = ch::geometry_kind_name(kind);
      std::visit(
          [&](const auto &geometry) {
            using Data = std::decay_t<decltype(geometry)>;
            const auto positions = [&](const auto &values) {
              append(values);
              std::array<double, 3> sum{};
              for (const auto &p : values) {
                sum[0] += p.x;
                sum[1] += p.y;
                sum[2] += p.z;
              }
              row.sums.push_back(sum);
              row.first.push_back(values.empty() ? std::array<double, 3>{}
                                                 : std::array<double, 3>{values[0].x, values[0].y, values[0].z});
            };
            row.points = geometry.positions.size();
            positions(geometry.positions);
            for (const auto &sample : geometry.motion.positions)
              positions(sample);
            row.materials = geometry.materials;
            for (const auto &key : row.materials)
              append(key);
            if constexpr (std::is_same_v<Data, ch::MeshData>) {
              append(geometry.triangles);
              append(geometry.triangle_material);
              append(geometry.corner_normals);
              append(geometry.corner_uv);
              append(geometry.corner_colors);
              for (const auto &sample : geometry.motion_corner_normals)
                append(sample);
              for (const auto &sample : geometry.motion_corner_uv)
                append(sample);
              for (const auto &sample : geometry.motion_corner_colors)
                append(sample);
            } else {
              append(geometry.radii);
              append(geometry.material_slots);
              for (const auto &sample : geometry.motion.radii)
                append(sample);
              if constexpr (std::is_same_v<Data, ch::CurveData>)
                append(geometry.first_key);
            }
          },
          item.data);
      observations.push_back(std::move(row));
    }
    const auto digest = XXH3_128bits_digest(hash);
    fingerprint = std::to_string(digest.high64) + ":" + std::to_string(digest.low64);
  }
  py::list rows = py::list(0);
  for (const auto &row : observations) {
    py::list samples = py::list(0);
    py::list materials = py::list(0);
    for (std::size_t i = 0; i < row.sums.size(); ++i) {
      const auto &a = row.first[i];
      const auto &b = row.sums[i];
      py::dict sample =
          py::dict(py::arg("first") = py::make_tuple(py::float_(a[0]), py::float_(a[1]), py::float_(a[2])),
                   py::arg("sums") = py::make_tuple(py::float_(b[0]), py::float_(b[1]), py::float_(b[2])));
      samples.append(sample);
    }
    for (const auto &key : row.materials) {
      py::str value = py::str(key.c_str());
      materials.append(value);
    }
    py::dict item = py::dict(py::arg("kind") = py::str(row.kind.c_str()), py::arg("points") = py::int_(row.points),
                             py::arg("samples") = samples, py::arg("materials") = materials);
    rows.append(item);
  }
  return py::dict(py::arg("fingerprint") = py::str(fingerprint.c_str()), py::arg("components") = rows,
                  py::arg("prepare_seconds") = py::float_(lease->seconds),
                  py::arg("cook_seconds") = py::float_(lease->preparation.cooking_seconds),
                  py::arg("conversion_seconds") = py::float_(stats.conversion_seconds),
                  py::arg("motion_cooks") = py::int_(lease->preparation.motion_cooks),
                  py::arg("sample_acquisition_seconds") = py::float_(lease->sample_seconds));
}

// Native typed-volume fixtures exercise representations that the legacy
// Volume SOP authors as separate scalar components instead of collated data.
py::object write_dense_volume_fixture(py::str path_arg, py::handle mode_arg) {
  const char *path;
  int mode = 0;
  const auto path_storage = path_arg.cast<std::string>();
  path = c_string_checked(path_storage);
  mode = argument_integer<int>(mode_arg);
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Dense fixture creation requires the host thread");
  GU_Detail detail;
  GA_RWHandleS names(detail.addStringTuple(GA_ATTRIB_PRIMITIVE, "name", 1));
  const auto add = [&]<typename Value>(const char *name, const Value &value) {
    auto *primitive = GU_PrimVolume::build(&detail);
    if constexpr (std::is_same_v<Value, UT_Vector3>)
      primitive->setStorageType(GEO_PrimVolume::VOLUME_TYPE_VECTOR3);
    else if constexpr (std::is_same_v<Value, UT_Vector2>)
      primitive->setStorageType(GEO_PrimVolume::VOLUME_TYPE_VECTOR2);
    else if constexpr (std::is_same_v<Value, UT_Vector4>)
      primitive->setStorageType(GEO_PrimVolume::VOLUME_TYPE_VECTOR4);
    else if constexpr (std::is_same_v<Value, int64>)
      primitive->setStorageType(GEO_PrimVolume::VOLUME_TYPE_INTEGER);
    else
      primitive->setStorageType(GEO_PrimVolume::VOLUME_TYPE_FLOAT);
    {
      auto writer = primitive->getVoxelWriteHandleByType<Value>();
      writer->size(19, 13, 11);
      writer->constant(value);
    }
    UT_Matrix3 shape;
    shape.identity();
    shape(0, 0) = 1;
    shape(1, 1) = .75f;
    shape(2, 2) = .5f;
    primitive->setTransform(shape);
    names.set(primitive->getMapOffset(), name);
    return primitive;
  };
  if (!(mode & 16)) {
    GEO_PrimVolume *density;
    if (mode & 1)
      density = add("density", int64(1));
    else
      density = add("density", mode & 64 ? std::numeric_limits<float>::quiet_NaN() : mode & 128 ? 0.f : .35f);
    if (mode & 8) {
      UT_Matrix3 shape;
      shape.identity();
      shape(0, 0) = 0;
      density->setTransform(shape);
    }
  }
  add("temperature", mode & 4 ? std::numeric_limits<float>::quiet_NaN() : 2.f);
  add("color", UT_Vector3(.15f, .35f, .7f));
  add("vel", UT_Vector3(1, 0, 0));
  add("custom_integer", int64(7));
  if (mode & 2) {
    add("unsupported_pair", UT_Vector2(1, 2));
    add("unsupported_quad", UT_Vector4(1, 2, 3, 4));
  }
  if (mode & 32)
    add("density", .7f);
  if (!detail.save(path, nullptr))
    throw std::runtime_error("Could not save dense volume fixture");
  return py::int_(detail.getNumPrimitives());
}

// A gridless VDB cannot safely pass through Houdini's geometry serializer.
// Exercise its native ownership/conversion boundary directly, then repair it.
py::object probe_gridless_volume() {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Volume probe requires the host thread");
  GU_Detail detail;
  auto *primitive = GU_PrimVDB::build(&detail);
  if (primitive->hasGrid())
    throw std::runtime_error("Expected an uninitialized VDB primitive");
  GA_RWHandleS names(detail.addStringTuple(GA_ATTRIB_PRIMITIVE, "name", 1));
  names.set(primitive->getMapOffset(), "density");
  ch::GeometryRequest request;
  request.sop_path = "gridless-probe";
  request.material_key = "probe";
  const auto convert = [&](std::vector<ch::SourceDiagnostic> &issues) {
    const auto partitions = ch::partition_geometry(detail);
    if (partitions.find(ch::NativeGeometryKind::Points))
      throw std::runtime_error("VDB anchors became loose particles");
    const auto prepared = ch::prepare_volume(detail, request, *partitions.find(ch::NativeGeometryKind::Volume), "probe",
                                             1, ch::native_motion_window(0, {}));
    return ch::convert_volume(*prepared, 1, nullptr, &issues);
  };
  std::vector<ch::SourceDiagnostic> issues;
  if (convert(issues) || issues.size() != 2)
    throw std::runtime_error("Gridless density did not recover locally");
  auto grid = openvdb::FloatGrid::create(0.f);
  grid->setName("density");
  grid->tree().setValue(openvdb::Coord(0), 1.f);
  auto *valid = GU_PrimVDB::buildFromGrid(detail, grid, nullptr, "density");
  const auto mixed = convert(issues);
  if (!mixed || mixed->fields.size() != 1 || issues.size() != 1)
    throw std::runtime_error("Gridless header hid valid density");
  // Repair the original header, retiring the temporary second primitive.
  detail.destroyPrimitive(*valid, true);
  primitive->setGrid(*grid);
  const auto repaired = convert(issues);
  if (!repaired || repaired->fields.size() != 1 || !issues.empty())
    throw std::runtime_error("Gridless repair retained warnings");
  return py::bool_(true);
}

py::object preserve_volume(py::str path_arg, py::handle time_arg) {
  const char *path;
  double time;
  const auto path_storage = path_arg.cast<std::string>();
  path = c_string_checked(path_storage);
  time = argument_real<double>(time_arg);
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Volume preparation requires the host thread");
  auto *sop = OPgetDirector()->findSOPNode(path);
  if (!sop)
    throw std::invalid_argument("Missing SOP for volume preservation");
  auto lease = std::make_unique<VolumeLease>();
  const auto started = std::chrono::steady_clock::now();
  OP_Context context(time);
  const auto handle = sop->getCookedGeoHandle(context);
  lease->cooking_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();
  if (!handle || sop->error() >= UT_ERROR_ABORT)
    throw std::runtime_error("Volume preservation cook failed");
  const auto acquired = std::chrono::steady_clock::now();
  const auto partitions = ch::partition_geometry(*handle.gdp());
  const auto *selection = partitions.find(ch::NativeGeometryKind::Volume);
  if (!selection)
    throw std::invalid_argument("Volume preservation requires volume primitives");
  ch::GeometryRequest request;
  request.sop_path = path;
  request.material_key = "probe";
  lease->prepared =
      ch::prepare_volume(*handle.gdp(), request, *selection, "volume-probe", 1, ch::native_motion_window(time, {}));
  lease->acquire_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - acquired).count();
  auto result = py::capsule(lease.get(), "cycles_houdini.VolumeLease", destroy_owned<VolumeLease>);
  if (result)
    lease.release();
  return result;
}

py::object convert_preserved_volume(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  const auto *lease = capsule_pointer<VolumeLease>(capsule, "cycles_houdini.VolumeLease");
  if (!lease)
    throw std::invalid_argument("Invalid volume lease");
  struct Observation {
    std::string name, type;
    std::uint64_t voxels{}, bytes{};
    std::array<double, 3> sum{}, centre{}, origin{}, spacing{};
  };
  std::vector<Observation> observations;
  std::string fingerprint;
  double conversion_seconds{}, inspection_seconds{};
  {
    ReleaseGIL release;
    const auto started = std::chrono::steady_clock::now();
    const auto data = ch::convert_volume(*lease->prepared);
    const auto converted = std::chrono::steady_clock::now();
    conversion_seconds = std::chrono::duration<double>(converted - started).count();
    auto *hash = XXH3_createState();
    if (!hash)
      throw std::bad_alloc();
    struct Cleanup {
      XXH3_state_t *value;
      ~Cleanup() { XXH3_freeState(value); }
    } cleanup{hash};
    XXH3_128bits_reset(hash);
    const auto append = [&](const auto &value) {
      static_assert(std::is_trivially_copyable_v<std::decay_t<decltype(value)>>);
      XXH3_128bits_update(hash, &value, sizeof(value));
    };
    const auto string = [&](const std::string &value) {
      append(value.size());
      XXH3_128bits_update(hash, value.data(), value.size());
    };
    for (const auto &field : data->fields) {
      const auto &grid = *field.grid;
      Observation row;
      row.name = field.name;
      row.type = grid.type();
      row.voxels = grid.activeVoxelCount();
      row.bytes = grid.memUsage();
      string(row.name);
      string(row.type);
      std::ostringstream metadata;
      grid.writeMeta(metadata);
      grid.transform().write(metadata);
      string(metadata.str());
      const auto origin = grid.transform().indexToWorld(openvdb::Vec3d(0)), spacing = grid.voxelSize();
      for (int axis = 0; axis < 3; ++axis) {
        row.origin[axis] = origin[axis];
        row.spacing[axis] = spacing[axis];
      }
      const auto inspect = [&]<typename Grid>(const Grid &typed) {
        const auto components = [](const auto &value) {
          if constexpr (std::is_arithmetic_v<std::decay_t<decltype(value)>>)
            return std::array<double, 3>{double(value), 0, 0};
          else
            return std::array<double, 3>{double(value[0]), double(value[1]), double(value[2])};
        };
        for (const auto component : components(typed.background()))
          append(component);
        row.centre = components(typed.tree().getValue(openvdb::Coord(0)));
        for (auto it = typed.tree().cbeginValueAll(); it; ++it) {
          const auto box = it.getBoundingBox();
          append(it.isValueOn());
          for (int axis = 0; axis < 3; ++axis) {
            append(box.min()[axis]);
            append(box.max()[axis]);
          }
          const auto value = components(*it);
          for (int axis = 0; axis < 3; ++axis) {
            append(value[axis]);
            if (it.isValueOn())
              row.sum[axis] += value[axis] * double(it.getVoxelCount());
          }
        }
      };
      if (grid.isType<openvdb::FloatGrid>())
        inspect(static_cast<const openvdb::FloatGrid &>(grid));
      else if (grid.isType<openvdb::Vec3SGrid>())
        inspect(static_cast<const openvdb::Vec3SGrid &>(grid));
      else
        throw std::invalid_argument("Ownership probe requires float or vec3-float grids");
      observations.push_back(std::move(row));
    }
    const auto value = XXH3_128bits_digest(hash);
    fingerprint = std::to_string(value.high64) + ":" + std::to_string(value.low64);
    inspection_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - converted).count();
  }
  py::list fields = py::list(0);
  for (const auto &row : observations) {
    py::dict item = py::dict(
        py::arg("name") = py::str(row.name.c_str()), py::arg("type") = py::str(row.type.c_str()),
        py::arg("active_voxels") = py::int_(row.voxels), py::arg("bytes") = py::int_(row.bytes),
        py::arg("active_sums") = py::make_tuple(py::float_(row.sum[0]), py::float_(row.sum[1]), py::float_(row.sum[2])),
        py::arg("voxel_zero") =
            py::make_tuple(py::float_(row.centre[0]), py::float_(row.centre[1]), py::float_(row.centre[2])),
        py::arg("origin") =
            py::make_tuple(py::float_(row.origin[0]), py::float_(row.origin[1]), py::float_(row.origin[2])),
        py::arg("voxel_size") =
            py::make_tuple(py::float_(row.spacing[0]), py::float_(row.spacing[1]), py::float_(row.spacing[2])));
    fields.append(item);
  }
  return py::dict(py::arg("fingerprint") = py::str(fingerprint.c_str()), py::arg("fields") = fields,
                  py::arg("cooking_seconds") = py::float_(lease->cooking_seconds),
                  py::arg("acquire_seconds") = py::float_(lease->acquire_seconds),
                  py::arg("conversion_seconds") = py::float_(conversion_seconds),
                  py::arg("inspection_seconds") = py::float_(inspection_seconds));
}

py::list packed_blender_references(py::str path_arg, double time) {
  if (!UT_Thread::isMainThread() || !std::isfinite(time))
    throw std::invalid_argument("Packed Blender discovery requires the host thread and a finite time");
  const auto path = py::cast<std::string>(path_arg);
  auto *sop = OPgetDirector()->findSOPNode(path.c_str());
  if (!sop)
    throw std::invalid_argument("Missing SOP for packed Blender discovery: " + path);
  OP_Context context(time);
  const auto handle = sop->getCookedGeoHandle(context);
  py::list result;
  std::set<std::int64_t> seen;
  std::set<std::pair<std::string, double>> references;
  std::function<void(const GU_ConstDetailHandle &, int)> scan;
  scan = [&](const GU_ConstDetailHandle &handle, int depth) {
    if (!handle || depth > 64)
      return;
    const ch::PreservedDetail lease(handle);
    const auto &detail = *lease.get();
    if (!seen.insert(detail.getUniqueId()).second)
      return;
    // Per-type tracking rejects ordinary meshes in constant time. Only owned
    // in-memory pack wrappers are traversed here: discovery must not force
    // lazy Alembic/disk/foreign procedural evaluation ahead of native capture.
    for (const auto type :
         {ch::PackedBlenderReference::typeId(), GU_PackedGeometry::typeId(), GU_PackedFragment::typeId()}) {
      if (!detail.countPrimitiveType(type))
        continue;
      UT_Array<const GA_Primitive *> primitives;
      detail.getPrimitivesOfType(type, primitives);
      for (const auto *primitive : primitives) {
        const auto *packed = static_cast<const GU_PrimPacked *>(primitive);
        if (const auto *blender = ch::packed_blender_reference(*packed))
          references.emplace(blender->reference(), blender->sourceFrame());
        else if (const auto *fragment = dynamic_cast<const GU_PackedFragment *>(packed->sharedImplementation()))
          scan(fragment->detailPtr(), depth + 1); // F3: a fragment's whole shared detail
        else
          scan(packed->getPackedDetail(), depth + 1);
      }
    }
  };
  scan(handle, 0);
  for (const auto &[reference, frame] : references)
    result.append(py::make_tuple(reference, frame));
  return result;
}

py::object preserve_packed_geometry(py::str path_arg, py::handle time_arg, py::handle material_arg) {
  const char *path, *material = nullptr;
  double time;
  const auto path_storage = path_arg.cast<std::string>();
  path = c_string_checked(path_storage);
  time = argument_real<double>(time_arg);
  std::string material_storage;
  if (!is_omitted(material_arg) && !material_arg.is_none()) {
    if (!py::isinstance<py::str>(material_arg))
      throw py::type_error("material must be a string or None");
    material_storage = material_arg.cast<std::string>();
    material = c_string_checked(material_storage);
  }
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Packed preparation requires the host thread");
  auto *sop = OPgetDirector()->findSOPNode(path);
  if (!sop)
    throw std::invalid_argument("Missing SOP for packed preservation");
  auto lease = std::make_unique<PackedLease>();
  const auto started = std::chrono::steady_clock::now();
  OP_Context context(time);
  const auto handle = sop->getCookedGeoHandle(context);
  lease->cooking_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();
  if (!handle || sop->error() >= UT_ERROR_ABORT)
    throw std::runtime_error("Packed preservation cook failed");
  const auto acquired = std::chrono::steady_clock::now();
  ch::GeometryRequest request;
  request.sop_path = path;
  request.material_key = "probe";
  request.material_mode = material ? ch::NativeMaterialMode::Native : ch::NativeMaterialMode::Uniform;
  if (material)
    request.object_material_path = material;
  const auto multiply = [](const ch::Matrix &a, const ch::Matrix &b) {
    ch::Matrix c{};
    for (int row = 0; row < 3; ++row)
      for (int col = 0; col < 4; ++col) {
        for (int k = 0; k < 3; ++k)
          c[row * 4 + col] += a[row * 4 + k] * b[k * 4 + col];
        if (col == 3)
          c[row * 4 + col] += a[row * 4 + 3];
      }
    return c;
  };
  std::set<std::int64_t> ancestors;
  std::function<void(std::shared_ptr<const ch::PreservedDetail>, ch::GeometryRequest, const ch::Matrix &, std::string,
                     int)>
      visit;
  visit = [&](auto retained, auto input, const auto &transform, std::string identity, int depth) {
    const auto &source = *retained->get();
    if (depth > 64 || !ancestors.insert(source.getUniqueId()).second)
      throw std::invalid_argument("Cyclic or excessive packed nesting in ownership probe");
    if (GU_PrimPacked::hasPackedPrimitives(source)) {
      if (GU_PrimPacked::countPackedPrimitives(source) != source.getNumPrimitives())
        throw std::invalid_argument("Packed ownership probe requires fully packed containers or mesh leaves");
      ch::NativeGeometrySelection selection;
      selection.kind = ch::NativeGeometryKind::Packed;
      selection.active = true;
      selection.full_detail = true;
      ch::NativePackedPreparationCache packed;
      const auto instances = packed.prepare(source, input, selection, lease->sources);
      for (const auto &instance : instances->values) {
        // This explicit ownership probe has no scene diagnostic output.
        // Keep its strict contract; native rendering handles recovery.
        if (instance.unavailable)
          throw std::invalid_argument(instance.unavailable->message);
        auto child = input;
        if (material)
          child.set_material_assignment(instance.material_assignment);
        visit(instance.prototype, std::move(child), multiply(transform, instance.transform),
              identity + "/packed:" + instance.identity, depth + 1);
      }
    } else {
      if (material)
        input.native_materials = ch::freeze_native_material_candidates(source, {input.material_assignment()});
      lease->leaves.push_back({std::move(retained), std::move(input), transform, std::move(identity)});
    }
    ancestors.erase(source.getUniqueId());
  };
  visit(lease->sources.retain(handle), request, ch::identity_matrix, "root", 0);
  lease->acquire_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - acquired).count();
  auto result = py::capsule(lease.get(), "cycles_houdini.PackedLease", destroy_owned<PackedLease>);
  if (result)
    lease.release();
  return result;
}

py::object convert_preserved_packed_geometry(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  const auto *lease = capsule_pointer<PackedLease>(capsule, "cycles_houdini.PackedLease");
  if (!lease)
    throw std::invalid_argument("Invalid packed lease");
  struct Observation {
    std::string identity;
    std::uint64_t prototype{}, triangles{};
    std::array<double, 3> first{}, minimum{}, maximum{};
    std::vector<std::string> materials;
  };
  std::vector<Observation> observations;
  std::string fingerprint;
  double seconds{};
  std::size_t variants{}, prototypes{};
  {
    ReleaseGIL release;
    const auto started = std::chrono::steady_clock::now();
    auto *hash = XXH3_createState();
    if (!hash)
      throw std::bad_alloc();
    struct Cleanup {
      XXH3_state_t *value;
      ~Cleanup() { XXH3_freeState(value); }
    } cleanup{hash};
    XXH3_128bits_reset(hash);
    const auto append = [&](const auto &values) {
      const auto size = values.size();
      XXH3_128bits_update(hash, &size, sizeof(size));
      XXH3_128bits_update(hash, values.data(), values.size() * sizeof(values[0]));
    };
    struct Mesh {
      ch::MeshData data;
      std::array<double, 3> minimum, maximum;
    };
    std::map<std::pair<std::int64_t, ch::NativeMaterialAssignment>, Mesh> meshes;
    std::set<std::int64_t> sources;
    for (const auto &leaf : lease->leaves) {
      const auto &source = *leaf.detail->get();
      const auto key = std::make_pair(source.getUniqueId(), leaf.request.material_assignment());
      sources.insert(source.getUniqueId());
      auto found = meshes.find(key);
      if (found == meshes.end()) {
        auto data = ch::capture_mesh(source, leaf.request, 1);
        if (data.positions.empty())
          throw std::invalid_argument("Packed ownership probe requires nonempty mesh leaves");
        append(data.positions);
        append(data.triangles);
        append(data.triangle_material);
        append(data.corner_normals);
        append(data.corner_uv);
        append(data.corner_colors);
        for (const auto &name : data.materials)
          append(name);
        Mesh mesh;
        mesh.minimum.fill(std::numeric_limits<double>::infinity());
        mesh.maximum.fill(-std::numeric_limits<double>::infinity());
        for (const auto &p : data.positions) {
          const double values[] = {p.x, p.y, p.z};
          for (int i = 0; i < 3; ++i) {
            mesh.minimum[i] = std::min(mesh.minimum[i], values[i]);
            mesh.maximum[i] = std::max(mesh.maximum[i], values[i]);
          }
        }
        mesh.data = std::move(data);
        found = meshes.emplace(key, std::move(mesh)).first;
      }
      const auto &mesh = found->second;
      Observation row;
      row.identity = leaf.identity;
      row.prototype = source.getUniqueId();
      row.triangles = mesh.data.triangles.size();
      row.materials = mesh.data.materials;
      append(row.identity);
      append(leaf.transform);
      const auto world = [&](const std::array<double, 3> &p) {
        std::array<double, 3> value{};
        for (int i = 0; i < 3; ++i) {
          value[i] = leaf.transform[i * 4 + 3];
          for (int j = 0; j < 3; ++j)
            value[i] += leaf.transform[i * 4 + j] * p[j];
        }
        return value;
      };
      const auto &first = mesh.data.positions.front();
      row.first = world({first.x, first.y, first.z});
      row.minimum.fill(std::numeric_limits<double>::infinity());
      row.maximum.fill(-std::numeric_limits<double>::infinity());
      for (int mask = 0; mask < 8; ++mask) {
        std::array<double, 3> corner{};
        for (int i = 0; i < 3; ++i)
          corner[i] = (mask & (1 << i)) ? mesh.maximum[i] : mesh.minimum[i];
        const auto p = world(corner);
        for (int i = 0; i < 3; ++i) {
          row.minimum[i] = std::min(row.minimum[i], p[i]);
          row.maximum[i] = std::max(row.maximum[i], p[i]);
        }
      }
      observations.push_back(std::move(row));
    }
    variants = meshes.size();
    prototypes = sources.size();
    const auto value = XXH3_128bits_digest(hash);
    fingerprint = std::to_string(value.high64) + ":" + std::to_string(value.low64);
    seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();
  }
  py::list leaves = py::list(0);
  for (const auto &row : observations) {
    py::list materials = py::list(0);
    for (const auto &key : row.materials) {
      py::str value = py::str(key.c_str());
      materials.append(value);
    }
    py::dict item = py::dict(
        py::arg("identity") = py::str(row.identity.c_str()), py::arg("prototype_id") = py::int_(row.prototype),
        py::arg("triangles") = py::int_(row.triangles), py::arg("materials") = materials,
        py::arg("first") = py::make_tuple(py::float_(row.first[0]), py::float_(row.first[1]), py::float_(row.first[2])),
        py::arg("minimum") =
            py::make_tuple(py::float_(row.minimum[0]), py::float_(row.minimum[1]), py::float_(row.minimum[2])),
        py::arg("maximum") =
            py::make_tuple(py::float_(row.maximum[0]), py::float_(row.maximum[1]), py::float_(row.maximum[2])));
    leaves.append(item);
  }
  return py::dict(py::arg("fingerprint") = py::str(fingerprint.c_str()), py::arg("instances") = leaves,
                  py::arg("prototype_meshes") = py::int_(static_cast<std::uint64_t>(prototypes)),
                  py::arg("material_variants") = py::int_(static_cast<std::uint64_t>(variants)),
                  py::arg("retained_details") = py::int_(static_cast<std::uint64_t>(lease->sources.size())),
                  py::arg("cooking_seconds") = py::float_(lease->cooking_seconds),
                  py::arg("acquire_seconds") = py::float_(lease->acquire_seconds),
                  py::arg("conversion_and_inspection_seconds") = py::float_(seconds));
}

py::object preserve_material_graphs(py::handle paths_arg, py::handle resolver_arg, py::handle time_arg) {
  py::handle paths, resolver;
  double time;
  paths = paths_arg;
  resolver = resolver_arg;
  time = argument_real<double>(time_arg);
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Material graph preparation requires the host thread");
  auto lease = std::make_unique<MaterialGraphLease>();
  for (auto value : sequence(paths)) {
    const auto path = string(value);
    if (lease->candidates.contains(path))
      continue;
    lease->candidates.emplace(path, ch::resolve_native_material(path));
  }
  ch::prepare_native_material_graphs(lease->graphs, lease->candidates, [&](const auto &reference) {
    return prepare_material_graph(resolver, reference, time);
  });
  auto result = py::capsule(lease.get(), "cycles_houdini.MaterialGraphLease", destroy_owned<MaterialGraphLease>);
  if (result)
    lease.release();
  return result;
}

py::object shader_value_object(const ch::ShaderValue &value) {
  return std::visit(
      [](const auto &input) -> py::object {
        using T = std::decay_t<decltype(input)>;
        if constexpr (std::is_same_v<T, bool>)
          return py::bool_(input);
        else if constexpr (std::is_same_v<T, int>)
          return py::int_(input);
        else if constexpr (std::is_same_v<T, float>)
          return py::float_(input);
        else if constexpr (std::is_same_v<T, std::string>)
          return py::str(input.data(), input.size());
        else if constexpr (std::is_same_v<T, std::array<float, 3>>)
          return py::make_tuple(py::float_(input[0]), py::float_(input[1]), py::float_(input[2]));
        else {
          py::list result = py::list(input.size());
          for (std::size_t i = 0; i < input.size(); ++i) {
            auto element = py::int_(input[i]);
            result[i] = element;
          }
          return result;
        }
      },
      value);
}

py::dict graph_value(const ch::ShaderGraphData &graph) {
  const auto endpoint = [](const ch::ShaderEndpoint &value) {
    return py::dict(py::arg("node") = py::str(value.node.c_str()), py::arg("socket") = py::str(value.socket.c_str()));
  };
  py::list nodes = py::list(0);
  py::list links = py::list(0);
  py::object surface = endpoint(graph.surface);
  for (const auto &node : graph.nodes) {
    py::dict parameters = py::dict();
    for (const auto &[key, value] : node.parameters) {
      py::object converted = shader_value_object(value);
      parameters[key.c_str()] = converted;
    }
    py::dict value =
        py::dict(py::arg("id") = py::str(node.id.c_str()), py::arg("definition") = py::str(node.definition.c_str()),
                 py::arg("parameters") = parameters);
    nodes.append(value);
  }
  for (const auto &link : graph.links) {
    py::object from = endpoint(link.from);
    py::object to = endpoint(link.to);
    py::dict value = py::dict(py::arg("from") = from, py::arg("to") = to);
    links.append(value);
  }
  const char *method = graph.displacement_method == ch::DisplacementMethod::True   ? "true"
                       : graph.displacement_method == ch::DisplacementMethod::Both ? "both"
                                                                                   : "bump";
  py::dict result = py::dict(py::arg("schema_version") = py::int_(graph.schema_version), py::arg("nodes") = nodes,
                             py::arg("links") = links, py::arg("displacement_method") = py::str(method));
  result["displacement_time_dependent"] = py::bool_(graph.displacement_time_dependent);
  if (graph.settings != ch::MaterialSettings{}) {
    // Only non-default settings are written, so graphs authored before these
    // settings existed keep exactly their previous dictionary form.
    const ch::MaterialSettings defaults;
    const auto &value = graph.settings;
    py::dict settings;
    if (value.emission_sampling != defaults.emission_sampling) {
      static const char *const names[] = {"none", "auto", "front", "back", "front_back"};
      settings["emission_sampling"] = py::str(names[int(value.emission_sampling)]);
    }
    if (value.transparent_shadows != defaults.transparent_shadows)
      settings["transparent_shadows"] = py::bool_(value.transparent_shadows);
    if (value.bump_map_correction != defaults.bump_map_correction)
      settings["bump_map_correction"] = py::bool_(value.bump_map_correction);
    if (value.volume_sampling != defaults.volume_sampling) {
      static const char *const names[] = {"distance", "equiangular", "multiple_importance"};
      settings["volume_sampling"] = py::str(names[int(value.volume_sampling)]);
    }
    if (value.volume_interpolation != defaults.volume_interpolation)
      settings["volume_interpolation"] =
          py::str(value.volume_interpolation == ch::VolumeInterpolation::Cubic ? "cubic" : "linear");
    if (value.volume_step_rate != defaults.volume_step_rate)
      settings["volume_step_rate"] = py::float_(value.volume_step_rate);
    result["settings"] = settings;
  }
  if (graph.image_time) {
    py::float_ time = py::float_(*graph.image_time);
    result["image_time"] = time;
  }
  if (!graph.surface.node.empty())
    result["surface"] = surface;
  for (const auto &[name, value] : {std::pair{"volume", graph.volume}, std::pair{"displacement", graph.displacement}})
    if (value) {
      py::object converted = endpoint(*value);
      result[name] = converted;
    }
  return result;
}

py::object convert_preserved_material_graphs(py::handle capsule_arg, py::handle paths_arg) {
  py::handle capsule, paths;
  capsule = capsule_arg;
  paths = paths_arg;
  const auto *lease = capsule_pointer<MaterialGraphLease>(capsule, "cycles_houdini.MaterialGraphLease");
  if (!lease)
    throw std::invalid_argument("Invalid material graph lease");
  std::vector<std::string> requested;
  for (auto path : sequence(paths))
    requested.push_back(string(path));
  std::vector<ch::MaterialData> materials;
  std::vector<std::byte> wire;
  {
    ReleaseGIL release;
    std::vector<ch::NativeMaterialReference> references;
    for (const auto &path : requested) {
      const auto found = lease->candidates.find(path);
      if (found == lease->candidates.end())
        throw std::invalid_argument("Material path was not frozen: " + path);
      references.push_back(found->second);
    }
    materials = ch::select_native_material_graphs(lease->graphs, references);
    ch::WorkerUpdate update;
    update.scene.materials = materials;
    wire = ch::encode_worker_update(update);
    if (ch::decode_worker_update(wire).scene.materials != materials)
      throw std::logic_error("Material wire round trip changed frozen graphs");
  }
  py::list values = py::list(0);
  py::object packet = bytes_value(wire);
  for (const auto &material : materials) {
    py::object graph = material.graph ? py::object(graph_value(*material.graph)) : py::object(py::none());
    py::dict value = py::dict(
        py::arg("key") = py::str(material.key.c_str()),
        py::arg("kind") = py::str(material.graph                                        ? "graph"
                                  : material.kind == ch::MaterialKind::PrincipledVolume ? "principled_volume"
                                                                                        : "diagnostic_surface"),
        py::arg("graph") = graph, py::arg("cryptomatte_name") = py::str(material.cryptomatte_name.c_str()),
        py::arg("pass_index") = py::int_(material.pass_index));
    if (!material.displacement_samples.empty()) {
      py::list samples = py::list(0);
      for (const auto &sample : material.displacement_samples) {
        py::object converted = graph_value(sample);
        samples.append(converted);
      }
      value["displacement_samples"] = samples;
    }
    values.append(value);
  }
  return py::dict(py::arg("materials") = values, py::arg("wire") = packet);
}

// Diagnostic entry points exercise the production geometry boundary. The
// baseline owns camera/color/texture policy; they are not an artist scheduler.
py::object preserve_native_scene(py::handle data_arg, py::handle time_arg, py::handle baseline_arg) {
  py::handle data, baseline;
  double time;
  data = data_arg;
  time = argument_real<double>(time_arg);
  baseline = baseline_arg;
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Native scene preparation requires the host thread");
  auto lease = std::make_unique<NativeSceneLease>();
  // A root-pose baseline (protocol 91) carries its moved roots outside the
  // object table; the lease edits the table, so give it every instance pose.
  lease->baseline = materialize_root_poses(snapshot(baseline));
  auto resolver = item(data, "material_resolver", false);
  const auto started = std::chrono::steady_clock::now();
  const auto prepare = [&](const ch::NativeMaterialLookup &candidates) {
    ch::prepare_native_material_graphs(lease->graphs, candidates, [&](const auto &reference) {
      return prepare_material_graph(resolver, reference, time);
    });
    std::set<std::string> fields;
    for (const auto &[path, reference] : candidates)
      if (const auto &material = lease->graphs.at(reference.key); material.graph) {
        const auto names = ch::shader_attribute_requests(*material.graph);
        fields.insert(names.begin(), names.end());
        const auto volume = ch::shader_volume_attribute_requests(*material.graph);
        fields.insert(volume.begin(), volume.end());
      }
    return fields;
  };
  const ch::NativeMaterialPreparation preparation{
      prepare, {}, {}, {}, [&](const std::string &key, bool displacement_only) {
        const auto found = lease->graphs.find(key);
        if (found == lease->graphs.end() || !found->second.graph)
          return false;
        const auto &graph = *found->second.graph;
        return displacement_only
                   ? (graph.displacement && ch::shader_pointiness_request(ch::displacement_snapshot(graph)))
                   : ch::shader_pointiness_request(graph);
      }};
  lease->prepared = lease->adapter.prepare(geometry_requests(item(data, "geometry")), time, true,
                                           motion_settings(item(data, "motion", false)), preparation);
  lease->prepare_seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();
  lease->cook_seconds = lease->adapter.stats().cooking_seconds;
  auto result = py::capsule(lease.get(), "cycles_houdini.NativeSceneLease", destroy_owned<NativeSceneLease>);
  if (result)
    lease.release();
  return result;
}

py::object convert_preserved_native_scene(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  auto *lease = capsule_pointer<NativeSceneLease>(capsule, "cycles_houdini.NativeSceneLease");
  if (!lease)
    throw std::invalid_argument("Invalid native scene lease");
  if (lease->consumed.exchange(true))
    throw std::logic_error("Native scene lease was already consumed");
  auto owned = std::make_shared<ch::OwnedSceneSnapshot>(*lease->baseline);
  double seconds{};
  ch::ExtractionStats stats;
  {
    ReleaseGIL release;
    const auto started = std::chrono::steady_clock::now();
    auto update = lease->adapter.convert(lease->prepared);
    lease->adapter.prepare_commit(update);
    update.materials = ch::select_native_material_graphs(lease->graphs, lease->adapter.material_references());
    owned->state = ch::snapshot_state(lease->baseline->state, update);
    lease->adapter.commit();
    stats = lease->adapter.stats();
    lease->prepared.reset();
    seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();
  }
  auto pointer = std::make_unique<SnapshotPtr>(std::move(owned));
  py::capsule result = py::capsule(pointer.get(), "cycles_houdini.SceneSnapshot", destroy_owned<SnapshotPtr>);
  pointer.release();
  return py::dict(
      py::arg("snapshot") = result, py::arg("prepare_seconds") = py::float_(lease->prepare_seconds),
      py::arg("cook_seconds") = py::float_(lease->cook_seconds), py::arg("convert_seconds") = py::float_(seconds),
      py::arg("extractions") = py::int_(stats.extractions), py::arg("motion_cooks") = py::int_(stats.motion_cooks));
}
// A parameter edit inside material networks (Entry.material_value_capture): resolve only the materials whose
// networks were edited and compare what they ask of the geometry with the committed scene. Returns
// (True, "", keys) and keeps the graphs for the next materials-only apply, or (False, reason, ()) when the
// complete read is needed: a material not on rendered geometry, a new material, a change in the attributes or
// pointiness the geometry capture carries, in true displacement (presence, method, time samples: they decide
// dicing and per-sample capture), in volume use (volume fields and media) or in the material kind.
py::object prepare_material_edit(py::handle capsule_arg, py::handle resolver_arg, py::handle paths_arg,
                                 py::handle time_arg) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Material graph preparation requires Houdini's host thread");
  auto &h = handle(capsule_arg);
  const double time = argument_real<double>(time_arg);
  h.material_edit.reset();
  const auto refuse = [](const std::string &reason) { return py::make_tuple(false, reason, py::tuple()); };
  if (!h.capture_only || h.capture_retired || !h.desired)
    return refuse("no accepted scene");
  std::set<std::string> edited;
  for (auto path : sequence(paths_arg))
    edited.insert(string(path));
  const auto committed = h.desired;
  const bool nested = committed->rendering && committed->rendering->nested_dielectrics;
  std::set<std::string> active_aovs;
  for (const auto &aov : committed->aovs ? *committed->aovs : h.config.aovs)
    active_aovs.insert(aov.name);
  ch::NativeMaterialGraphTable graphs;
  std::set<std::string> keys;
  for (const auto &reference : h.adapter.material_references()) {
    const auto paths = ch::native_material_paths(reference);
    if (std::none_of(paths.begin(), paths.end(), [&](const auto &path) { return edited.contains(path); }))
      continue;
    ch::NativeMaterialLookup candidate;
    candidate.emplace(reference.assignment(), reference);
    ch::prepare_native_material_graphs(
        graphs, candidate, [&](const auto &value) { return prepare_material_graph(resolver_arg, value, time); });
    keys.insert(reference.key);
  }
  if (keys.empty())
    return refuse("the edited material is not on rendered geometry");
  const auto true_displacement = [](const ch::MaterialData &m) {
    return m.graph && m.graph->displacement && m.graph->displacement_method != ch::DisplacementMethod::Bump;
  };
  for (const auto &key : keys) {
    const auto old = committed->materials.find(key);
    if (old == committed->materials.end())
      return refuse("a material without an accepted graph: " + key);
    const auto &a = old->second;
    const auto &b = graphs.at(key);
    if (a.kind != b.kind || a.color_attribute != b.color_attribute)
      return refuse("material kind or colour attribute changed: " + key);
    for (const bool displacement_only : {false, true}) {
      if (material_geometry_fields(a, displacement_only, nested) !=
          material_geometry_fields(b, displacement_only, nested))
        return refuse("attributes the geometry carries changed: " + key);
      if (material_geometry_pointiness(a, displacement_only, active_aovs) !=
          material_geometry_pointiness(b, displacement_only, active_aovs))
        return refuse("pointiness changed: " + key);
    }
    if (true_displacement(a) != true_displacement(b) ||
        (true_displacement(a) && (a.graph->displacement_method != b.graph->displacement_method ||
                                  a.graph->displacement_time_dependent != b.graph->displacement_time_dependent)) ||
        a.displacement_samples.size() != b.displacement_samples.size())
      return refuse("true displacement changed: " + key);
    if (bool(a.graph && a.graph->volume) != bool(b.graph && b.graph->volume))
      return refuse("volume use changed: " + key);
  }
  Handle::MaterialEdit stash{committed, time, {}};
  py::list names;
  for (const auto &key : keys) {
    stash.graphs.emplace(key, graphs.at(key));
    names.append(key);
  }
  h.material_edit = std::move(stash);
  return py::make_tuple(true, std::string(), py::tuple(names));
}

} // namespace cycles_houdini::python_detail
