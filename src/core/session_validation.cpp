// SPDX-License-Identifier: Apache-2.0
#include "session_internal.h"
#include "util/tbb.h"
#include <algorithm>
#include <atomic>
#include <cstdlib>
#include <string_view>

namespace cycles_houdini {
namespace {
// Pure per-element checks of large arrays on all cores (the engine's TBB pool,
// fixed ranges). Each call answers one check; callers still decide the checks
// in their serial order, so the first failing check's message is thrown as
// before. Arrays up to 65536 elements and CYCLES_HOUDINI_SERIAL_CONVERSION=1
// stay on the calling thread.
template <class Values, class Invalid> bool any_invalid(const Values &values, const Invalid &invalid) {
  constexpr std::size_t grain = 65536;
  const std::size_t count = values.size();
  const auto *data = values.data();
  const auto *serial = std::getenv("CYCLES_HOUDINI_SERIAL_CONVERSION");
  if (count <= grain || (serial && std::string_view(serial) == "1")) {
    for (std::size_t i = 0; i < count; ++i)
      if (invalid(data[i]))
        return true;
    return false;
  }
  // Fixed blocks of 65536 elements from index 0 (the partitioner never chooses
  // boundaries); the answer is a plain "any", so it cannot depend on scheduling.
  std::atomic<bool> found{false};
  const std::size_t blocks = (count + grain - 1) / grain;
  tbb::parallel_for(tbb::blocked_range<std::size_t>(0, blocks, 1), [&](const tbb::blocked_range<std::size_t> &r) {
    for (auto b = r.begin(); b != r.end(); ++b) {
      if (found.load(std::memory_order_relaxed))
        return;
      for (auto i = b * grain, end = std::min(count, i + grain); i < end; ++i)
        if (invalid(data[i])) {
          found.store(true, std::memory_order_relaxed);
          return;
        }
    }
  });
  return found.load();
}
const auto not_finite3 = [](const Float3 &p) { return !finite(p); };
const auto not_finite2 = [](const Float2 &v) { return !std::isfinite(v.x) || !std::isfinite(v.y); };
} // namespace
void RenderSession::Impl::validate(const SceneUpdate &u, std::map<std::string, Float3> &volume_bounds,
                                   const std::map<std::string, ShaderGraphRequirements> &graph_requirements,
                                   const std::map<BindingKey, ShaderGraphRequirements> &recovered,
                                   const VolumeRecovery &volume_fallbacks, std::vector<ObjectRecord *> &pose_records) {
  const auto binding_requirements = [&](const std::string &key, bool has_uv, bool has_color, bool strands = false,
                                        const AttributeLayout &attributes = AttributeLayout{},
                                        const AttributeLayout &instances =
                                            AttributeLayout{}) -> const ShaderGraphRequirements * {
    if (auto found = recovered.find(binding_key(key, has_uv, has_color, strands, attributes, instances));
        found != recovered.end())
      return &found->second;
    if (auto found = graph_requirements.find(key); found != graph_requirements.end())
      return &found->second;
    return nullptr;
  };
  struct Keys {
    std::unordered_set<std::string_view> added, deleted;
  };
  auto unique = [](const auto &added, const auto &deleted, const char *kind, auto key) {
    // Views are local to validation; every getter returns a reference
    // into the immutable candidate. Do not copy each instance path.
    Keys keys;
    keys.added.reserve(added.size());
    keys.deleted.reserve(deleted.size());
    for (const auto &k : deleted)
      if (k.empty() || !keys.deleted.insert(k).second)
        throw std::invalid_argument(std::string("Invalid or duplicate deleted ") + kind + " key");
    for (const auto &value : added) {
      const auto &k = key(value);
      if (k.empty() || !keys.added.insert(k).second)
        throw std::invalid_argument(std::string("Invalid or duplicate ") + kind + " key");
      if (keys.deleted.contains(k))
        throw std::invalid_argument(std::string("Cannot delete and update the same ") + kind +
                                    " in one transaction: " + k);
    }
    return keys;
  };
  unique(u.materials, u.deleted_materials, "material", [](const auto &m) -> const std::string & { return m.key; });
  unique(u.meshes, u.deleted_geometry, "geometry", [](const auto &m) -> const std::string & {
    if (!m)
      throw std::invalid_argument("Null mesh data");
    return m->key;
  });
  unique(u.volumes, u.deleted_geometry, "volume", [](const auto &v) -> const std::string & {
    if (!v)
      throw std::invalid_argument("Null volume data");
    return v->key;
  });
  unique(u.points, u.deleted_geometry, "point cloud", [](const auto &p) -> const std::string & {
    if (!p)
      throw std::invalid_argument("Null point data");
    return p->key;
  });
  unique(u.curves, u.deleted_geometry, "curve", [](const auto &c) -> const std::string & {
    if (!c)
      throw std::invalid_argument("Null curve data");
    return c->key;
  });
  const auto object_keys =
      unique(u.objects, u.deleted_objects, "object", [](const auto &o) -> const std::string & { return o.key; });
  const bool ordered_poses = std::is_sorted(u.object_poses.begin(), u.object_poses.end(),
                                            [](const auto &a, const auto &b) { return a.key < b.key; });
  // Native differences are ordered. Adjacent keys prove uniqueness below;
  // no large hash table or repeated path hashing is needed for that case.
  // Arbitrary internal callers still receive full unsorted-key validation.
  if (!ordered_poses)
    unique(u.object_poses, std::vector<std::string>{}, "object pose",
           [](const auto &p) -> const std::string & { return p.key; });
  unique(u.lights, u.deleted_lights, "light", [](const auto &l) -> const std::string & { return l.key; });
  auto available_materials = std::set<std::string>{};
  auto definitions = material_definitions;
  auto available_meshes = std::set<std::string>{};
  for (const auto &[k, v] : materials)
    available_materials.insert(k);
  for (const auto &[k, v] : meshes)
    available_meshes.insert(k);
  for (const auto &k : u.deleted_materials) {
    available_materials.erase(k);
    definitions.erase(k);
  }
  for (const auto &k : u.deleted_geometry)
    available_meshes.erase(k);
  std::set<std::string> geometry_updates;
  const auto geometry_type = [&](const std::string &key, GeometryKind kind) {
    if (!geometry_updates.insert(key).second)
      throw std::invalid_argument("A geometry key cannot identify multiple incoming resources: " + key);
    if (auto old = meshes.find(key); old != meshes.end() && old->second.kind != kind)
      throw std::invalid_argument("Use a distinct resource key when changing geometry type: " + key);
  };
  for (const auto &mesh : u.meshes)
    geometry_type(mesh->key, GeometryKind::Mesh);
  for (const auto &volume : u.volumes)
    geometry_type(volume->key, GeometryKind::Volume);
  for (const auto &points : u.points)
    geometry_type(points->key, GeometryKind::Points);
  for (const auto &curves : u.curves)
    geometry_type(curves->key, GeometryKind::Curves);
  for (const auto &m : u.materials) {
    validate_pass_index(m.pass_index);
    validate_cryptomatte_name(m.cryptomatte_name);
    if ((m.kind == MaterialKind::Graph) != m.graph.has_value())
      throw std::invalid_argument("Graph materials require a graph and other material kinds cannot contain one");
    if (m.kind == MaterialKind::Graph && !m.color_attribute.empty())
      throw std::invalid_argument("Shader graphs must author color attributes explicitly in their nodes");
    if (m.key.empty() || !finite(m.color) || !std::isfinite(m.roughness) || !std::isfinite(m.metallic) ||
        !std::isfinite(m.emission_strength) || !std::isfinite(m.density) || m.density < 0 ||
        !std::isfinite(m.anisotropy) || std::abs(m.anisotropy) >= 1 || !finite(m.volume_emission_color) ||
        !std::isfinite(m.volume_emission_strength) || m.volume_emission_strength < 0 ||
        !std::isfinite(m.blackbody_intensity) || m.blackbody_intensity < 0 || m.blackbody_intensity > 1 ||
        !std::isfinite(m.temperature_kelvin_scale) || m.temperature_kelvin_scale <= 0)
      throw std::invalid_argument("Invalid material");
    available_materials.insert(m.key);
    definitions[m.key] = m;
  }
  const auto is_volume_material = [&](const std::string &key) {
    const auto &material = definitions.at(key);
    return material.kind == MaterialKind::VolumeAbsorption || material.kind == MaterialKind::PrincipledVolume ||
           (material.kind == MaterialKind::Graph && graph_requirements.at(key).volume);
  };
  const auto is_implicit_grid_material = [&](const std::string &key) {
    return is_volume_material(key) && definitions.at(key).kind != MaterialKind::Graph;
  };
  const auto true_displacement = [&](const std::string &key) {
    const auto &material = definitions.at(key);
    return material.graph && material.graph->displacement &&
           material.graph->displacement_method != DisplacementMethod::Bump;
  };
  const auto blur_enabled = u.motion_blur.value_or(session->scene->integrator->get_motion_blur());
  const auto &requested = u.passes.value_or(config.passes);
  if (blur_enabled && std::find(requested.begin(), requested.end(), "motion") != requested.end())
    throw std::invalid_argument("Cycles motion vectors require shutter motion blur disabled");
  const auto validate_mesh_displacement = [&](const MeshData &mesh) {
    for (const auto &key : mesh.materials) {
      const auto *required =
          binding_requirements(key, !mesh.corner_uv.empty(), !mesh.corner_colors.empty(), false,
                               attribute_layout(mesh.attributes, mesh.instance_attributes), mesh.instance_attributes);
      if (required && required->volume && !required->volume_fields.empty())
        throw std::invalid_argument("Closed mesh media use shader density, not sparse-grid fields: " + mesh.key);
      if (required && (required->displacement_requires_uv || required->requires_uv) && mesh.corner_uv.empty())
        throw std::invalid_argument("Shader requires corner UVs: " + mesh.key);
    }
  };
  const auto validate_volume_material = [&](const VolumeData &volume) {
    if (volume_fallbacks.contains(volume.key))
      return;
    const auto &material = definitions.at(volume.material);
    const auto has = [&](const std::string &name) {
      return std::any_of(volume.fields.begin(), volume.fields.end(),
                         [&](const auto &field) { return field.name == name; });
    };
    if (material.kind == MaterialKind::PrincipledVolume) {
      if (material.blackbody_intensity > 0 && !has("temperature"))
        throw std::invalid_argument("Blackbody volume emission requires a temperature field");
      if (material.volume_emission_strength > 0 && !material.emission_field.empty() &&
          !volume_shader_field_error(volume, material.emission_field).empty())
        throw std::invalid_argument("Missing volume emission field: " + material.emission_field);
    }
    if (material.kind == MaterialKind::Graph) {
      const auto &required = graph_requirements.at(volume.material);
      if (required.surface)
        throw std::invalid_argument("Sparse grids require a volume-only material: " + volume.material);
      for (const auto &name : required.volume_fields) {
        const auto issue = volume_shader_field_error(volume, name, required.scalar_volume_fields.contains(name),
                                                     required.vector_volume_fields.contains(name));
        if (!issue.empty())
          throw std::invalid_argument(issue + " in " + volume.key);
      }
    }
  };
  for (const auto &m : u.meshes) {
    if (!m || m->key.empty())
      throw std::invalid_argument("Mesh requires a key and owned data");
    available_meshes.insert(m->key);
    validate_subdivision(*m);
    const auto corners = m->subdivision ? m->subdivision->face_corners.size() : m->triangles.size() * 3;
    if ((!m->corner_normals.empty() && m->corner_normals.size() != corners) ||
        (!m->corner_uv.empty() && m->corner_uv.size() != corners) ||
        (!m->corner_colors.empty() && m->corner_colors.size() != corners) ||
        (!m->triangle_material.empty() && m->triangle_material.size() != m->triangles.size()))
      throw std::invalid_argument("Mesh attribute size mismatch: " + m->key);
    if (any_invalid(m->positions, not_finite3))
      throw std::invalid_argument("Non-finite vertex: " + m->key);
    if (!m->pointiness.empty() && m->pointiness.size() != m->positions.size())
      throw std::invalid_argument("Pointiness vertex ownership mismatch: " + m->key);
    if (any_invalid(m->pointiness, [](float value) { return !std::isfinite(value) || value < 0 || value > 1; }))
      throw std::invalid_argument("Invalid owned pointiness value: " + m->key);
    if (any_invalid(m->corner_normals, not_finite3))
      throw std::invalid_argument("Non-finite normal: " + m->key);
    validate_named_attributes(m->attributes, corners, m->motion.positions.size());
    validate_instance_attribute_layout(m->instance_attributes, m->attributes);
    if (any_invalid(m->corner_colors, not_finite3))
      throw std::invalid_argument("Non-finite mesh color: " + m->key);
    if (any_invalid(m->corner_uv, not_finite2))
      throw std::invalid_argument("Non-finite UV: " + m->key);
    if (const int points = static_cast<int>(m->positions.size());
        any_invalid(m->triangles, [points](const Triangle &t) {
          return t.a < 0 || t.a >= points || t.b < 0 || t.b >= points || t.c < 0 || t.c >= points;
        }))
      throw std::invalid_argument("Triangle index outside mesh: " + m->key);
    for (const auto &k : m->materials) {
      if (!available_materials.contains(k))
        throw std::invalid_argument("Missing material: " + k);
      if (is_implicit_grid_material(k) &&
          !recovered.contains(binding_key(k, !m->corner_uv.empty(), !m->corner_colors.empty(), false,
                                          attribute_layout(m->attributes, m->instance_attributes),
                                          m->instance_attributes)))
        throw std::invalid_argument(
            "Polygon meshes require a native shader graph; implicit grid materials need sparse density fields: " + k);
    }
    if (const int slots = static_cast<int>(m->materials.size());
        any_invalid(m->triangle_material, [slots](int i) { return i < 0 || i >= slots; }))
      throw std::invalid_argument("Invalid material slot: " + m->key);
    validate_geometry_motion(m->motion, m->positions.size(), false);
    if (!m->motion.positions.empty() && !m->corner_normals.empty() &&
        m->motion_corner_normals.size() != m->motion.positions.size())
      throw std::invalid_argument("Split normals require matching deformation motion samples");
    if (!m->motion_corner_normals.empty() &&
        (m->corner_normals.empty() || m->motion_corner_normals.size() != m->motion.positions.size()))
      throw std::invalid_argument("Motion corner normals must match base normals and position steps");
    for (const auto &sample : m->motion_corner_normals) {
      if (sample.size() != corners)
        throw std::invalid_argument("Motion corner normal size mismatch");
      if (any_invalid(sample, not_finite3))
        throw std::invalid_argument("Non-finite motion normal");
    }
    const auto sampled_attribute = [&](const auto &centre, const auto &samples, bool complete,
                                       const auto finite_value) {
      if (!samples.empty() && (!complete || centre.empty() || samples.size() != m->motion.positions.size()))
        throw std::invalid_argument("Motion mesh attributes must match base attributes and position steps");
      for (const auto &sample : samples) {
        if (sample.size() != corners)
          throw std::invalid_argument("Motion mesh attribute size mismatch");
        if (any_invalid(sample, [&](const auto &value) { return !finite_value(value); }))
          throw std::invalid_argument("Non-finite motion mesh attribute");
      }
    };
    sampled_attribute(m->corner_uv, m->motion_corner_uv, m->motion_uv_complete,
                      [](Float2 v) { return std::isfinite(v.x) && std::isfinite(v.y); });
    sampled_attribute(m->corner_colors, m->motion_corner_colors, m->motion_color_complete,
                      [](Float3 v) { return finite(v); });
    if (m->nominal_pose) {
      const auto &pose = *m->nominal_pose;
      validate_named_attributes(pose.attributes, corners);
      if (attribute_layout(pose.attributes) != attribute_layout(m->attributes))
        throw std::invalid_argument("Nominal named attributes must match the base field layout");
      if (pose.positions.size() != m->positions.size() || pose.corner_normals.size() != m->corner_normals.size() ||
          pose.corner_uv.size() != m->corner_uv.size() || pose.corner_colors.size() != m->corner_colors.size())
        throw std::invalid_argument("Nominal mesh pose must match base topology and attributes: " + m->key);
      for (const auto *values : {&pose.positions, &pose.corner_normals, &pose.corner_colors})
        for (const auto p : *values)
          if (!finite(p))
            throw std::invalid_argument("Non-finite nominal mesh pose: " + m->key);
      for (const auto uv : pose.corner_uv)
        if (!std::isfinite(uv.x) || !std::isfinite(uv.y))
          throw std::invalid_argument("Non-finite nominal mesh UV: " + m->key);
    }
    if (!m->texture_space.empty() && m->texture_space.size() != 2)
      throw std::invalid_argument("Mesh texture space must hold a location and a size: " + m->key);
    for (const auto p : m->texture_space)
      if (!finite(p))
        throw std::invalid_argument("Non-finite mesh texture space: " + m->key);
    if (!m->blender_vertex_normals.empty() && m->blender_vertex_normals.size() != m->positions.size())
      throw std::invalid_argument("Blender vertex normals must match the mesh points: " + m->key);
    if (m->blender_vertex_normals.empty() && !m->blender_edges.empty())
      throw std::invalid_argument("Blender edges need Blender vertex normals: " + m->key);
    if (m->blender_edges.size() % 2)
      throw std::invalid_argument("Blender edges must be vertex pairs: " + m->key);
    for (const int point : m->blender_edges)
      if (point < 0 || std::size_t(point) >= m->positions.size())
        throw std::invalid_argument("Blender edge vertex out of range: " + m->key);
    for (const auto n : m->blender_vertex_normals)
      if (!finite(n))
        throw std::invalid_argument("Non-finite Blender vertex normal: " + m->key);
    validate_mesh_displacement(*m);
  }
  for (const auto &volume : u.volumes) {
    if (volume->key.empty() || volume->fields.empty() || volume->step_size < 0 || !std::isfinite(volume->step_size))
      throw std::invalid_argument("Invalid sparse volume");
    if (!available_materials.contains(volume->material))
      throw std::invalid_argument("Missing volume material: " + volume->material);
    if (!volume_fallbacks.contains(volume->key) && !is_volume_material(volume->material))
      throw std::invalid_argument("Sparse volume requires a volume material: " + volume->material);
    std::set<std::string> fields;
    std::set<std::uint64_t> field_versions;
    for (const auto &field : volume->fields) {
      if (!fields.insert(field.name).second)
        throw std::invalid_argument("Duplicate sparse field: " + field.name);
      if (field.revision && !field_versions.insert(field.revision).second)
        throw std::invalid_argument("Duplicate owned volume field version");
      if (const auto reason = volume_field_error(field); !reason.empty())
        throw std::invalid_argument(reason);
    }
    if (!fields.contains("density"))
      throw std::invalid_argument("Initial sparse volume shader requires a density field");
    const auto bound = volume_velocity_bounds(*volume);
    volume_bounds[volume->key] = {bound[0], bound[1], bound[2]};
    validate_volume_material(*volume);
    available_meshes.insert(volume->key);
  }
  const auto validate_strands = [&](const auto &data) {
    const auto size = data.positions.size();
    if (size > std::size_t(std::numeric_limits<int>::max()) || data.radii.size() != size ||
        (!data.colors.empty() && data.colors.size() != size))
      throw std::invalid_argument("Point/curve attribute size mismatch: " + data.key);
    if (any_invalid(data.positions, not_finite3))
      throw std::invalid_argument("Non-finite point/curve position: " + data.key);
    if (any_invalid(data.radii, [](float r) { return !std::isfinite(r) || r < 0; }))
      throw std::invalid_argument("Point/curve radius must be finite and nonnegative: " + data.key);
    validate_named_attributes(data.attributes, size, data.motion.positions.size());
    validate_instance_attribute_layout(data.instance_attributes, data.attributes);
    const auto sampled_field = [&](const auto &centre, const auto &samples, bool complete) {
      if (!samples.empty() && (!complete || centre.empty() || samples.size() != data.motion.positions.size()))
        throw std::invalid_argument("Point/curve field samples must match geometry motion");
      for (const auto &values : samples) {
        if (values.size() != centre.size())
          throw std::invalid_argument("Point/curve field sample size mismatch");
        if (any_invalid(values, [](const auto &v) {
              if constexpr (std::is_same_v<std::decay_t<decltype(v)>, Float3>)
                return !finite(v);
              else
                return !std::isfinite(v.x) || !std::isfinite(v.y);
            }))
          throw std::invalid_argument("Non-finite point/curve field sample");
      }
    };
    sampled_field(data.colors, data.motion_colors, data.motion_color_complete);
    sampled_field(data.uv, data.motion_uv, data.motion_uv_complete);
    if (any_invalid(data.colors, not_finite3))
      throw std::invalid_argument("Non-finite point/curve color: " + data.key);
    if (any_invalid(data.randoms, [](float random) { return !std::isfinite(random) || random < 0 || random > 1; }))
      throw std::invalid_argument("Invalid point/curve identity value: " + data.key);
    if (any_invalid(data.uv, not_finite2))
      throw std::invalid_argument("Non-finite point/curve UV: " + data.key);
    if (!data.material.empty() && !data.materials.empty())
      throw std::invalid_argument("Point/curve palettes cannot also specify a uniform material: " + data.key);
    if (!data.material_slots.empty() && data.materials.empty())
      throw std::invalid_argument("Point/curve shader slots require an explicit material palette: " + data.key);
    std::size_t elements = size;
    if constexpr (requires { data.first_key; })
      elements = data.first_key.size();
    if (!data.material_slots.empty() && data.material_slots.size() != elements)
      throw std::invalid_argument("Point/curve shader slot count mismatch: " + data.key);
    for (const auto slot : data.material_slots)
      if (slot < 0 || std::size_t(slot) >= data.materials.size())
        throw std::invalid_argument("Invalid point/curve shader slot: " + data.key);
    for (const auto &material : strand_materials(data)) {
      if (material.empty()) {
        bool intrinsic_default = false;
        if constexpr (requires { data.splats; })
          intrinsic_default = data.splats.has_value();
        if (!data.materials.empty() && !intrinsic_default)
          throw std::invalid_argument("Point/curve material palette contains an empty key: " + data.key);
        continue;
      }
      if (!available_materials.contains(material))
        throw std::invalid_argument("Missing material: " + material);
      const auto *required =
          binding_requirements(material, !data.uv.empty(), !data.colors.empty(), true,
                               attribute_layout(data.attributes, data.instance_attributes), data.instance_attributes);
      if ((required && required->volume) || (!required && is_volume_material(material)))
        throw std::invalid_argument("Points and hair require a surface material: " + material);
      if (required) {
        if (true_displacement(material) && required->displacement)
          throw std::invalid_argument(
              "True displacement requires polygon meshes; point/hair deformation needs a separate adapter: " +
              material);
        if (required->requires_uv && data.uv.empty())
          throw std::invalid_argument("Shader requires point/curve UVs: " + data.key);
        if (required->requires_mesh_tangents)
          throw std::invalid_argument("Tangent Normal Map requires a polygon mesh: " + data.key);
      }
    }
    available_meshes.insert(data.key);
    validate_geometry_motion(data.motion, size, true);
  };
  for (const auto &points : u.points) {
    validate_strands(*points);
    if (points->splats) {
      const auto &g = *points->splats;
      const auto size = points->positions.size();
      if (g.scene_linear_color && !g.harmonics.empty())
        throw std::invalid_argument("Higher Gaussian harmonics require sRGB SH encoding");
      if (g.scales.size() != size || g.rotations.size() != size || g.radiance.size() != size ||
          (!g.harmonics.empty() && g.harmonics.size() != size))
        throw std::invalid_argument("Gaussian splat attribute size mismatch: " + points->key);
      const auto check_scale = [&](const auto &values) {
        for (const auto v : values)
          if (!finite(v) || v.x < 0 || v.y < 0 || v.z < 0 ||
              std::max({v.x, v.y, v.z}) > std::numeric_limits<float>::max() / 4)
            throw std::invalid_argument("Invalid or unbounded Gaussian scale: " + points->key);
      };
      const auto check_rotation = [&](const auto &values) {
        for (const auto &q : values) {
          double length = 0;
          for (float v : q) {
            if (!std::isfinite(v))
              throw std::invalid_argument("Nonfinite Gaussian rotation");
            length += double(v) * v;
          }
          if (std::abs(length - 1) > 1e-4)
            throw std::invalid_argument("Gaussian rotation must be normalized: " + points->key);
        }
      };
      const auto check_radiance = [&](const auto &values) {
        for (const auto &v : values) {
          for (float c : v)
            if (!std::isfinite(c))
              throw std::invalid_argument("Nonfinite Gaussian radiance");
          if (v[3] < 0 || v[3] > 1)
            throw std::invalid_argument("Gaussian opacity must be in 0..1");
        }
      };
      check_scale(g.scales);
      check_rotation(g.rotations);
      check_radiance(g.radiance);
      for (const auto &coefficients : g.harmonics)
        for (const auto v : coefficients)
          if (!finite(v))
            throw std::invalid_argument("Nonfinite Gaussian harmonic coefficient");
      const auto samples = [&](const auto &values, const auto &check) {
        if (!values.empty() && values.size() != points->motion.positions.size())
          throw std::invalid_argument("Gaussian motion sample count mismatch");
        for (const auto &sample : values) {
          if (sample.size() != size)
            throw std::invalid_argument("Gaussian motion attribute size mismatch");
          check(sample);
        }
      };
      samples(g.motion_scales, check_scale);
      samples(g.motion_rotations, check_rotation);
      samples(g.motion_radiance, check_radiance);
      // Static and shutter-sampled ellipsoids must have representable
      // finite support before touching the accepted engine scene.
      const auto bounds = [&](const auto &positions, const auto &scales) {
        for (std::size_t point = 0; point < size; ++point) {
          const auto p = positions[point], scale = scales[point];
          const double extent = double(std::max({scale.x, scale.y, scale.z})) * 3.035;
          if (std::abs(double(p.x)) + extent > std::numeric_limits<float>::max() ||
              std::abs(double(p.y)) + extent > std::numeric_limits<float>::max() ||
              std::abs(double(p.z)) + extent > std::numeric_limits<float>::max())
            throw std::invalid_argument("Gaussian splat bounds exceed finite coordinates: " + points->key);
        }
      };
      bounds(points->positions, g.scales);
      for (std::size_t step = 0; step < points->motion.positions.size(); ++step)
        bounds(points->motion.positions[step], g.motion_scales.empty() ? g.scales : g.motion_scales[step]);
    }
    if ((!points->randoms.empty() && points->randoms.size() != points->positions.size()) ||
        (!points->uv.empty() && points->uv.size() != points->positions.size()))
      throw std::invalid_argument("Point attribute size mismatch: " + points->key);
    if (!points->lifetimes.empty() && points->lifetimes.size() != points->positions.size())
      throw std::invalid_argument("Point lifetime size mismatch: " + points->key);
    for (const auto interval : points->lifetimes)
      if (!std::isfinite(interval.x) || !std::isfinite(interval.y) || interval.x < 0 || interval.y > 1 ||
          interval.x > interval.y)
        throw std::invalid_argument("Point lifetime must be an ordered finite normalized shutter interval: " +
                                    points->key);
  }
  for (const auto &curves : u.curves) {
    validate_strands(*curves);
    if (curves->shape != CurveShape::Linear && curves->shape != CurveShape::Thick &&
        curves->shape != CurveShape::Ribbon)
      throw std::invalid_argument("Unsupported curve shape: " + curves->key);
    if (curves->first_key.size() > std::size_t(std::numeric_limits<int>::max()) ||
        (!curves->randoms.empty() && curves->randoms.size() != curves->first_key.size()) ||
        (!curves->uv.empty() && curves->uv.size() != curves->first_key.size()) ||
        (!curves->generated.empty() && curves->generated.size() != curves->first_key.size()) ||
        (!curves->generated.empty() && !curves->generated_attribute) ||
        (curves->first_key.empty() != curves->positions.empty()))
      throw std::invalid_argument("Curve attribute size mismatch: " + curves->key);
    for (const auto &value : curves->generated)
      if (!finite(value))
        throw std::invalid_argument("Non-finite curve Generated coordinate: " + curves->key);
    for (std::size_t index = 0; index < curves->first_key.size(); ++index) {
      const int first = curves->first_key[index];
      const auto end =
          index + 1 < curves->first_key.size() ? curves->first_key[index + 1] : int(curves->positions.size());
      if ((index == 0 && first != 0) || first < 0 || end - first < 2 || end > int(curves->positions.size()))
        throw std::invalid_argument("Each linear curve needs at least two ordered keys: " + curves->key);
    }
  }
  std::map<std::string, const AttributeLayout *> incoming_instance_layouts;
  for (const auto &data : u.meshes)
    incoming_instance_layouts.emplace(data->key, &data->instance_attributes);
  for (const auto &data : u.points)
    incoming_instance_layouts.emplace(data->key, &data->instance_attributes);
  for (const auto &data : u.curves)
    incoming_instance_layouts.emplace(data->key, &data->instance_attributes);
  const auto validate_instance_binding = [&](const std::string &geometry, const std::vector<UniformAttribute> &fields) {
    validate_uniform_attributes(fields);
    const AttributeLayout *expected = nullptr;
    if (const auto found = incoming_instance_layouts.find(geometry); found != incoming_instance_layouts.end())
      expected = found->second;
    else if (const auto found = meshes.find(geometry); found != meshes.end())
      expected = &found->second.instance_attributes;
    if (expected && !expected->empty()) {
      const auto supplied = attribute_layout(fields);
      for (const auto &[name, components] : *expected)
        if (const auto found = supplied.find(name); found == supplied.end() || found->second != components)
          throw std::invalid_argument("Owned object does not supply its declared instance field: " + name);
    }
  };
  for (const auto &o : u.objects) {
    validate_matrix(o.transform);
    if (o.nominal_transform)
      validate_matrix(*o.nominal_transform);
    validate_transform_motion(o.motion, o.transform);
    validate_object_properties(o.properties);
    if (!available_meshes.contains(o.geometry_key))
      throw std::invalid_argument("Missing geometry: " + o.geometry_key);
    validate_instance_binding(o.geometry_key, o.attributes);
  }
  if (!incoming_instance_layouts.empty())
    for (const auto &[key, object] : objects)
      if (incoming_instance_layouts.contains(object.geometry_key) && !object_keys.deleted.contains(key) &&
          !object_keys.added.contains(key))
        validate_instance_binding(object.geometry_key, object.attributes);
  // Many ordered poses (a moved instancer): validate in chunks on all cores. Each chunk starts its own walk
  // at its first key; every slot is written by one task. Any problem re-runs the serial loop below, which
  // reports the same first error as before.
  bool poses_validated = false;
  if (pose_fast_rule() && ordered_poses && u.object_poses.size() >= 8192 && object_keys.added.empty() &&
      object_keys.deleted.empty()) {
    const std::size_t count = u.object_poses.size(), chunks = 64, grain = (count + chunks - 1) / chunks;
    std::vector<ObjectRecord *> records(count, nullptr);
    std::atomic<bool> failed{false};
    tbb::parallel_for(tbb::blocked_range<std::size_t>(0, chunks, 1), [&](const tbb::blocked_range<std::size_t> &range) {
      for (std::size_t chunk = range.begin(); chunk != range.end(); ++chunk) {
        const std::size_t begin = chunk * grain, end = std::min(count, begin + grain);
        if (begin >= end || failed.load(std::memory_order_relaxed))
          continue;
        auto object = objects.lower_bound(u.object_poses[begin].key);
        for (std::size_t n = begin; n < end; ++n) {
          const auto &pose = u.object_poses[n];
          bool ok = !pose.key.empty() && (n == 0 || u.object_poses[n - 1].key != pose.key);
          if (ok)
            try {
              validate_matrix(pose.transform);
              if (pose.nominal_transform)
                validate_matrix(*pose.nominal_transform);
              validate_transform_motion(pose.motion, pose.transform);
            } catch (const std::exception &) {
              ok = false;
            }
          while (ok && object != objects.end() && object->first < pose.key)
            ++object;
          if (!ok || object == objects.end() || object->first != pose.key || !object->second.node) {
            failed = true;
            break;
          }
          records[n] = &object->second;
        }
      }
    });
    if (!failed.load()) {
      pose_records = std::move(records);
      poses_validated = true;
    }
  }
  pose_records.reserve(u.object_poses.size());
  auto pose_object = objects.begin();
  const std::string *previous_pose_key = nullptr;
  if (!poses_validated)
    for (const auto &pose : u.object_poses) {
      if (pose.key.empty() || (ordered_poses && previous_pose_key && *previous_pose_key == pose.key))
        throw std::invalid_argument("Invalid or duplicate object pose key");
      previous_pose_key = &pose.key;
      // contains() may hash even for an empty set. Ordinary pose-only
      // commands have no upserts/deletions, so skip those lookups outright.
      if ((!object_keys.added.empty() && object_keys.added.contains(pose.key)) ||
          (!object_keys.deleted.empty() && object_keys.deleted.contains(pose.key)))
        throw std::invalid_argument("Object pose conflicts with an upsert/deletion in the same transaction: " +
                                    pose.key);
      validate_matrix(pose.transform);
      if (pose.nominal_transform)
        validate_matrix(*pose.nominal_transform);
      validate_transform_motion(pose.motion, pose.transform);
      if (ordered_poses)
        while (pose_object != objects.end() && pose_object->first < pose.key)
          ++pose_object;
      const auto object = ordered_poses ? pose_object : objects.find(pose.key);
      if (object == objects.end() || object->first != pose.key)
        throw std::invalid_argument("Pose references a missing object: " + pose.key);
      if (!object->second.node)
        throw std::logic_error("Pose has no owned engine object: " + pose.key);
      // Retained geometry is checked below if a resource is removed.
      // A pose cannot rebind it, so ordinary moves need no extra lookup.
      pose_records.push_back(&object->second);
    }
  // Retained bindings were validated when accepted. Only geometry removal
  // can invalidate them; object replacement/deletion in this transaction
  // supersedes its old binding. Camera and transform edits need no global
  // object-map allocation or rescan. Newly rebound objects were checked above.
  if (!u.deleted_geometry.empty())
    for (const auto &[key, object] : objects)
      if (!available_meshes.contains(object.geometry_key) && !object_keys.deleted.contains(key) &&
          !object_keys.added.contains(key))
        throw std::invalid_argument("Missing geometry: " + object.geometry_key);
  for (const auto &[k, m] : meshes)
    if (available_meshes.contains(k) && !geometry_updates.contains(k))
      for (const auto &material : m.materials)
        if (!material.empty() && !available_materials.contains(material))
          throw std::invalid_argument("Material is still bound: " + material);
  for (const auto &[key, record] : meshes)
    if (available_meshes.contains(key) && !geometry_updates.contains(key) && record.kind != GeometryKind::Volume) {
      for (const auto &material : record.materials)
        if (!material.empty()) {
          const bool strands = record.kind != GeometryKind::Mesh;
          if (is_volume_material(material) && (strands || is_implicit_grid_material(material)) &&
              !recovered.contains(binding_key(material, record.has_uv, record.has_color, strands, record.attributes,
                                              record.instance_attributes)))
            throw std::invalid_argument(record.kind == GeometryKind::Mesh
                                            ? "A polygon mesh is still bound to this surface material: " + material
                                            : "Points and hair require a surface material: " + material);
          if (record.kind != GeometryKind::Mesh)
            if (auto *required = binding_requirements(material, record.has_uv, record.has_color, true,
                                                      record.attributes, record.instance_attributes)) {
              if (true_displacement(material) && required->displacement)
                throw std::invalid_argument("True displacement requires polygon meshes; points or hair remain bound: " +
                                            material);
              if (required->requires_uv && !record.has_uv)
                throw std::invalid_argument("Shader requires point/curve UVs: " + key);
              if (required->requires_mesh_tangents)
                throw std::invalid_argument("Tangent Normal Map requires a polygon mesh: " + key);
            }
        }
      if (record.source)
        validate_mesh_displacement(*record.source);
    }
  for (const auto &[key, record] : meshes)
    if (record.volume && available_meshes.contains(key) &&
        std::none_of(u.volumes.begin(), u.volumes.end(), [&](const auto &volume) { return volume->key == key; })) {
      const auto definition = definitions.find(record.volume->material);
      if (definition != definitions.end() && !volume_fallbacks.contains(key) &&
          !is_volume_material(record.volume->material))
        throw std::invalid_argument("A sparse volume is still bound to this material");
      if (definition != definitions.end())
        validate_volume_material(*record.volume);
    }
  for (const auto &light : u.lights) {
    if (light.source_path.size() > 4096 || light.source_path.find('\0') != std::string::npos)
      throw std::invalid_argument("Invalid light source path");
    if (light.linking_id.size() > 4096 || light.linking_id.find('\0') != std::string::npos)
      throw std::invalid_argument("Invalid light linking identity");
    validate_matrix(light.transform);
    validate_lightgroup_assignment(light.lightgroup);
    validate_cryptomatte_name(light.cryptomatte_object);
    validate_cryptomatte_name(light.cryptomatte_asset);
    if (light.cone &&
        (!std::isfinite(light.cone->inner_half_angle) || !std::isfinite(light.cone->outer_half_angle) ||
         !std::isfinite(light.cone->rolloff) || light.cone->inner_half_angle < 0 ||
         light.cone->outer_half_angle < light.cone->inner_half_angle || light.cone->outer_half_angle > 3.141592654f ||
         light.cone->rolloff <= 0 || light.kind == LightKind::Sun))
      throw std::invalid_argument("Invalid light cone");
    if (light.key.empty() || !finite(light.strength) || light.radius < 0 || !std::isfinite(light.radius) ||
        light.size_u <= 0 || light.size_v <= 0 || !std::isfinite(light.size_u) || !std::isfinite(light.size_v) ||
        !std::isfinite(light.angle) || light.angle < 0 || light.angle > 3.141592654f || !std::isfinite(light.spread) ||
        light.spread < 0 || light.spread > 3.141592654f || !std::isfinite(light.spot_smooth) || light.spot_smooth < 0 ||
        light.spot_smooth > 1 || light.max_bounces < 0 || light.max_bounces > 1024 ||
        (light.kind != LightKind::Point && light.kind != LightKind::Area && light.kind != LightKind::Sun &&
         light.kind != LightKind::Spot))
      throw std::invalid_argument("Invalid light");
  }
  const auto validate_camera = [&](const CameraData &lens) {
    validate_matrix(lens.transform);
    if (!std::isfinite(lens.aperture_size) || lens.aperture_size < 0 || !std::isfinite(lens.focus_distance) ||
        lens.focus_distance <= 0 || !std::isfinite(lens.aperture_rotation) || !std::isfinite(lens.aperture_ratio) ||
        lens.aperture_ratio <= 0 ||
        (lens.aperture_blades != 0 && (lens.aperture_blades < 3 || lens.aperture_blades > 32)))
      throw std::invalid_argument("Invalid camera depth of field");
    if (!lens.intrinsic_motion.empty() && (lens.intrinsic_motion.size() < 3 || lens.intrinsic_motion.size() > 129 ||
                                           lens.intrinsic_motion.size() % 2 != 1))
      throw std::invalid_argument("Camera intrinsic motion requires odd 3..129 complete samples");
    const CameraIntrinsicSample nominal_intrinsic{lens.short_axis_fov, lens.near_clip, lens.far_clip, lens.ortho_scale,
                                                  lens.panorama};
    if (const auto *error = camera_intrinsic_error(nominal_intrinsic, lens.orthographic, config.width, config.height,
                                                   lens.pixel_aspect))
      throw std::invalid_argument(error);
    for (const auto &sample : lens.intrinsic_motion) {
      if (bool(sample.panorama) != bool(lens.panorama) ||
          (sample.panorama &&
           (sample.panorama->type != lens.panorama->type ||
            ((lens.panorama->type == "fisheye_equisolid" || lens.panorama->type == "fisheye_lens_polynomial") &&
             sample.panorama->sensor_fit != lens.panorama->sensor_fit))))
        throw std::invalid_argument("Camera intrinsic motion changed a discrete projection control");
      if (const auto *error =
              camera_intrinsic_error(sample, lens.orthographic, config.width, config.height, lens.pixel_aspect))
        throw std::invalid_argument(error);
    }
    if (!lens.dof_motion.empty() &&
        (lens.dof_motion.size() < 3 || lens.dof_motion.size() > 129 || lens.dof_motion.size() % 2 != 1))
      throw std::invalid_argument("Camera depth-of-field motion requires odd 3..129 complete samples");
    double max_aperture = lens.aperture_size, min_ratio = lens.aperture_ratio,
           min_focus = std::max(lens.focus_distance, 1e-5f);
    for (const auto &sample : lens.dof_motion) {
      if (!std::isfinite(sample.aperture_size) || sample.aperture_size < 0 || !std::isfinite(sample.focus_distance) ||
          sample.focus_distance <= 0 || !std::isfinite(sample.aperture_rotation) ||
          !std::isfinite(sample.aperture_ratio) || sample.aperture_ratio <= 0)
        throw std::invalid_argument("Invalid camera depth-of-field motion sample");
      max_aperture = std::max(max_aperture, double(sample.aperture_size));
      min_ratio = std::min(min_ratio, double(sample.aperture_ratio));
      min_focus = std::min(min_focus, double(std::max(sample.focus_distance, 1e-5f)));
    }
    // Conservative extrema also bound every linearly interpolated
    // sample. Validate the intermediate divisions and origin bounds,
    // rather than only individually finite source values.
    const double limit = std::numeric_limits<float>::max(), inverse_ratio = 1 / min_ratio;
    const double radius = max_aperture * std::max(1.0, inverse_ratio) * (lens.image_iris_method ? std::sqrt(2.0) : 1.0),
                 slope = radius / min_focus;
    double max_near = lens.near_clip;
    for (const auto &sample : lens.intrinsic_motion)
      max_near = std::max(max_near, double(sample.near_clip));
    const double extension = radius + std::max(max_near, slope * max_near);
    if (inverse_ratio > limit || radius > limit || slope > limit || !std::isfinite(extension) || extension > limit)
      throw std::invalid_argument("Camera depth-of-field intermediates exceed the engine numeric range");
    if (!std::isfinite(lens.pixel_aspect) || lens.pixel_aspect <= 0 ||
        !(lens.short_axis_fov > 0 && lens.short_axis_fov < 3.14159f) || !std::isfinite(lens.short_axis_fov) ||
        !std::isfinite(lens.near_clip) || !std::isfinite(lens.far_clip) || !std::isfinite(lens.ortho_scale) ||
        lens.near_clip <= 0 || lens.far_clip <= lens.near_clip || lens.ortho_scale <= 0)
      throw std::invalid_argument("Invalid camera");
    if (!std::isfinite(lens.shutter_frames) || lens.shutter_frames <= 0)
      throw std::invalid_argument("Camera shutter duration must be finite and positive");
    const auto &sampling = lens.sampling;
    if (!std::isfinite(sampling.rolling_duration) || sampling.rolling_duration < 0 || sampling.rolling_duration > 1)
      throw std::invalid_argument("Camera rolling shutter duration must be finite and in [0,1]");
    if (!sampling.shutter_curve.empty() &&
        (sampling.shutter_curve.size() < 2 || sampling.shutter_curve.size() > 256 ||
         std::any_of(sampling.shutter_curve.begin(), sampling.shutter_curve.end(),
                     [](float value) { return !std::isfinite(value) || value < 0 || value > 1; }) ||
         std::none_of(sampling.shutter_curve.begin(), sampling.shutter_curve.end(),
                      [](float value) { return value > 0; })))
      throw std::invalid_argument(
          "Camera shutter curve requires 2..256 nonnegative normalized weights with positive exposure");
    if (lens.panorama) {
      if (lens.orthographic)
        throw std::invalid_argument("Panoramic and orthographic projection cannot be combined");
      if (const auto *error = panorama_error(*lens.panorama))
        throw std::invalid_argument(error);
      if (lens.panorama->type == "fisheye_equisolid" || lens.panorama->type == "fisheye_lens_polynomial")
        (void)panorama_sensor(*lens.panorama, config.width, config.height, lens.pixel_aspect);
    }
    validate_camera_transform_motion(lens.motion, lens.transform);
  };
  if (u.camera)
    validate_camera(*u.camera);
  if (u.dicing) {
    if (!std::isfinite(u.dicing->offscreen_scale) || u.dicing->offscreen_scale < 1)
      throw std::invalid_argument("Offscreen Scale must be finite and at least 1");
    if (u.dicing->camera)
      validate_camera(*u.dicing->camera);
  }
  if (u.crop) {
    const auto &r = *u.crop;
    if (r.x < 0 || r.y < 0 || r.width <= 0 || r.height <= 0 || r.width > config.width - r.x ||
        r.height > config.height - r.y)
      throw std::invalid_argument("Crop is outside the image");
  }
  if (u.background && !finite(*u.background))
    throw std::invalid_argument("Non-finite background");
  if (u.world) {
    const auto &world = *u.world;
    if (world.visibility > 255 || world.max_bounces < 0 || world.max_bounces > 1024 ||
        (world.map_resolution != 0 && (world.map_resolution < 16 || world.map_resolution > 16384)))
      throw std::invalid_argument("Invalid world visibility, maximum bounces or map resolution (Auto 0, or 16..16384)");
    if (world.volume_sampling < ccl::VOLUME_SAMPLING_DISTANCE ||
        world.volume_sampling > ccl::VOLUME_SAMPLING_MULTIPLE_IMPORTANCE)
      throw std::invalid_argument("Invalid world volume sampling method");
  }
  if (u.background_lightgroup)
    validate_lightgroup_assignment(*u.background_lightgroup);
  if (u.world && (u.world->source_path.size() > 4096 || u.world->source_path.find('\0') != std::string::npos))
    throw std::invalid_argument("World diagnostic source path is malformed or too long");
  if (u.transparent_roughness_threshold &&
      (!std::isfinite(*u.transparent_roughness_threshold) || *u.transparent_roughness_threshold < 0 ||
       *u.transparent_roughness_threshold > 1))
    throw std::invalid_argument("Transparent glass roughness threshold must be finite and in [0,1]");
}

bool RenderSession::Impl::validate_displacement_motion(const SceneUpdate &incoming, const SceneUpdate &u,
                                                       const std::vector<ObjectRecord *> &pose_records) {
  auto &i = *this;
  const bool object_edits = !u.objects.empty() || !u.object_poses.empty();
  const bool temporal_validation =
      i.has_temporal_materials || std::any_of(incoming.materials.begin(), incoming.materials.end(),
                                              [](const auto &m) { return !m.displacement_samples.empty(); });
  if (temporal_validation && u.materials.empty() && u.meshes.empty() && object_edits) {
    // A transform edit checks only its referenced prototypes. Do not build
    // an all-object map while a large instance scene is being manipulated.
    std::map<std::string, std::size_t> counts;
    const auto validate_motion = [&](const std::string &geometry_key, const std::vector<Matrix> &motion) {
      if (!counts.contains(geometry_key)) {
        std::size_t count = 0;
        const auto &record = i.meshes.at(geometry_key);
        if (record.source)
          for (const auto &key : record.materials)
            count = std::max(count, i.material_definitions.at(key).displacement_samples.size());
        counts[geometry_key] = count;
      }
      const auto count = counts.at(geometry_key);
      if (count && !motion.empty() && motion.size() != count)
        throw std::invalid_argument("Displacement and object shutter sample counts must match");
    };
    for (const auto &object : u.objects)
      validate_motion(object.geometry_key, object.motion);
    for (std::size_t n = 0; n < u.object_poses.size(); ++n)
      validate_motion(pose_records[n]->geometry_key, u.object_poses[n].motion);
  } else if (temporal_validation && (!u.materials.empty() || !u.meshes.empty() || object_edits)) {
    std::map<std::string, const MaterialData *> definitions;
    for (const auto &[key, value] : i.material_definitions)
      definitions[key] = &value;
    for (const auto &value : u.materials)
      definitions[value.key] = &value;
    std::map<std::string, const MeshData *> geometry;
    for (const auto &[key, record] : i.meshes)
      if (record.source)
        geometry[key] = record.source.get();
    for (const auto &key : u.deleted_geometry)
      geometry.erase(key);
    for (const auto &value : u.meshes)
      geometry[value->key] = value.get();
    std::map<std::string, std::size_t> counts;
    for (const auto &[key, mesh] : geometry) {
      std::size_t count = 0;
      for (const auto &material : mesh->materials) {
        const auto size = definitions.at(material)->displacement_samples.size();
        if (size && count && size != count)
          throw std::invalid_argument("Displacement shutter graphs need matching sample counts on a mesh");
        count = std::max(count, size);
      }
      if (count && !mesh->motion.positions.empty() && mesh->motion.positions.size() + 1 != count)
        throw std::invalid_argument("Displacement and geometry shutter sample counts must match");
      counts[key] = count;
    }
    std::map<std::string, std::pair<std::string, std::size_t>> objects;
    for (const auto &[key, value] : i.objects)
      objects[key] = {value.geometry_key, value.motion.size()};
    for (const auto &key : u.deleted_objects)
      objects.erase(key);
    for (const auto &value : u.objects)
      objects[value.key] = {value.geometry_key, value.motion.size()};
    for (const auto &pose : u.object_poses)
      objects.at(pose.key).second = pose.motion.size();
    for (const auto &[key, value] : objects)
      if (value.second && counts[value.first] && value.second != counts[value.first])
        throw std::invalid_argument("Displacement and object shutter sample counts must match");
  }
  return object_edits;
}

} // namespace cycles_houdini
