// SPDX-License-Identifier: Apache-2.0
#include "blend_mesh.h"
#include <pybind11/pybind11.h>
#include <algorithm>
#include <bit>
#include <cmath>
#include <cstring>
#include <limits>
#include <numeric>
#include <set>
#include <stdexcept>

namespace py = pybind11;
namespace cycles_houdini {
namespace {
py::object field(py::handle object, const char *key, bool required = true) {
  if (!object || !py::isinstance<py::dict>(object))
    throw std::invalid_argument("Blender mesh records must be dictionaries");
  // Public 3.13 storage lookup bypasses subclass hooks and owns the result
  // before any subsequent Python numeric/buffer conversion can mutate a row.
  PyObject *value = nullptr;
  const int found = PyDict_GetItemStringRef(object.ptr(), key, &value);
  if (found < 0)
    throw py::error_already_set();
  if (!found) {
    if (required)
      throw std::invalid_argument(std::string("Missing Blender mesh field: ") + key);
    return {};
  }
  return py::reinterpret_steal<py::object>(value);
}
std::string text(py::handle value) {
  if (!value || !py::isinstance<py::str>(value))
    throw std::invalid_argument("Blender mesh field must be a string");
  auto result = py::cast<std::string>(value);
  return result.substr(0, result.find('\0'));
}
bool flag(py::handle record, const char *key, bool fallback) {
  auto value = field(record, key, false);
  if (!value || value.is_none())
    return fallback;
  if (!py::isinstance<py::bool_>(value))
    throw std::invalid_argument(std::string("Blender mesh field must be a boolean: ") + key);
  return py::cast<bool>(value);
}
std::string named(py::handle record) {
  auto value = field(record, "native_name", false);
  return text(value ? value : field(record, "name"));
}
std::vector<py::object> list(py::handle object) {
  if (!object)
    return {};
  if (!py::isinstance<py::list>(object) && !py::isinstance<py::tuple>(object))
    throw std::invalid_argument("Blender mesh fields must be lists");
  std::vector<py::object> result;
  const auto count = py::len(object);
  result.reserve(count);
  for (py::size_t index = 0; index < count; ++index) {
    if (py::isinstance<py::list>(object))
      result.push_back(py::reinterpret_borrow<py::list>(object)[index]);
    else
      result.push_back(py::reinterpret_borrow<py::tuple>(object)[index]);
  }
  return result;
}
// Strict native-endian typed buffers prevent interpreting an arbitrary byte
// stream or a strided numpy view as geometry. memcpy also avoids alignment and
// aliasing assumptions about the Python exporter's allocation.
struct Buffer {
  py::buffer_info view;
  std::string name;
  char kind{};
  std::size_t size{};
  Buffer(py::handle object, std::string label, char expected, int width) : name(std::move(label)) {
    try {
      if (!py::isinstance<py::buffer>(object))
        throw std::invalid_argument("Blender mesh array must be a contiguous typed buffer: " + name);
      view = py::reinterpret_borrow<py::buffer>(object).request();
    } catch (py::error_already_set &) {
      throw std::invalid_argument("Blender mesh array must be a contiguous typed buffer: " + name);
    }
    py::ssize_t stride = view.itemsize;
    for (py::ssize_t dimension = view.ndim; view.size && dimension-- > 0;) {
      if (view.shape[dimension] > 1 && view.strides[dimension] != stride)
        throw std::invalid_argument("Blender mesh array must be a contiguous typed buffer: " + name);
      stride *= view.shape[dimension];
    }
    try {
      std::string format = view.format;
      if (!format.empty() && (format[0] == '@' || format[0] == '=' || format[0] == '<' || format[0] == '>')) {
        if ((format[0] == '<' && std::endian::native != std::endian::little) ||
            (format[0] == '>' && std::endian::native != std::endian::big))
          throw std::invalid_argument("Blender mesh array has non-native byte order: " + name);
        format.erase(0, 1);
      }
      // Windows numpy exports int32 as C long ('l'), while array.array
      // and other exporters commonly use C int ('i'). The item size is
      // authoritative: a platform's 64-bit long remains invalid here.
      const bool compatible = format.size() == 1 && (format[0] == expected || (expected == 'i' && format[0] == 'l'));
      if (!compatible || view.itemsize != (expected == 'B' ? 1 : 4) || view.size < 0 || width < 1 ||
          (view.ndim != 1 && view.ndim != 2))
        throw std::invalid_argument("Blender mesh array has the wrong numeric type or shape: " + name);
      kind = expected;
      size = std::size_t(view.size);
      if (size % std::size_t(width) || (view.ndim == 2 && (view.shape.size() < 2 || view.shape[1] != width)))
        throw std::invalid_argument("Blender mesh array has the wrong tuple width: " + name);
      if (size / std::size_t(width) > std::size_t(std::numeric_limits<int>::max()))
        throw std::invalid_argument("Blender mesh exceeds signed indexing limits: " + name);
    } catch (...) {
      view = py::buffer_info{};
      throw;
    }
  }
  Buffer(const Buffer &) = delete;
  Buffer &operator=(const Buffer &) = delete;
  template <class T> T at(std::size_t i) const {
    T value{};
    if (i >= size)
      throw std::invalid_argument("Blender mesh array index exceeds " + name);
    std::memcpy(&value, static_cast<const char *>(view.ptr) + i * sizeof(T), sizeof(T));
    return value;
  }
  float number(std::size_t i) const {
    const auto value = kind == 'f'   ? at<float>(i)
                       : kind == 'i' ? float(at<std::int32_t>(i))
                                     : float(at<std::uint8_t>(i));
    if (!std::isfinite(value))
      throw std::invalid_argument("Blender mesh array contains non-finite values: " + name);
    return value;
  }
  void count(std::size_t expected) const {
    if (size != expected)
      throw std::invalid_argument("Blender mesh array length mismatch: " + name);
  }
};
py::object array(py::handle arrays, py::handle key) {
  if (!py::isinstance<py::dict>(arrays))
    throw std::invalid_argument("Blender mesh arrays must be a dictionary");
  // This overload preserves object-key lookup while taking an owned result,
  // using the same narrowly scoped Python 3.13 dictionary storage API.
  PyObject *value = nullptr;
  const int found = PyDict_GetItemRef(arrays.ptr(), key.ptr(), &value);
  if (found < 0)
    throw py::error_already_set();
  if (!found)
    throw std::invalid_argument("Missing Blender mesh array: " + text(key));
  return py::reinterpret_steal<py::object>(value);
}
py::object array(py::handle arrays, py::handle fields, const char *name) { return array(arrays, field(fields, name)); }
int integer(const Buffer &b, std::size_t index, int limit, const char *domain) {
  const auto result = b.at<std::int32_t>(index);
  if (result < 0 || result >= limit)
    throw std::invalid_argument(std::string("Blender mesh has an invalid ") + domain + " index");
  return result;
}
Float3 normal(Float3 n) {
  const auto length = std::sqrt(double(n.x) * n.x + double(n.y) * n.y + double(n.z) * n.z);
  if (length == 0)
    return {};
  return {float(n.x / length), float(n.y / length), float(n.z / length)};
}
SubdivisionSettings subdivision(py::handle record) {
  SubdivisionSettings result;
  auto settings = field(record, "subdivision", false);
  if (!settings || settings.is_none())
    return result;
  const auto choose = [&](const char *key, const std::vector<std::string> &choices, int fallback) {
    auto value = field(settings, key, false);
    if (!value)
      return fallback;
    const auto name = text(value);
    const auto found = std::find(choices.begin(), choices.end(), name);
    if (found == choices.end())
      throw std::invalid_argument(std::string("Invalid Blender subdivision ") + key);
    return int(found - choices.begin());
  };
  result.type = static_cast<SubdivisionType>(
      choose("type", {"none", "linear", "catmull_clark"}, int(SubdivisionType::CatmullClark)));
  result.boundary = static_cast<SubdivisionBoundary>(
      choose("boundary", {"none", "edge_only", "edge_and_corner"}, int(result.boundary)));
  result.fvar = static_cast<SubdivisionFVar>(choose(
      "fvar", {"none", "corners_only", "corners_plus1", "corners_plus2", "boundaries", "all"}, int(result.fvar)));
  result.adaptive = false;
  result.space = SubdivisionSpace::Object;
  result.dicing_rate = 1;
  if (auto value = field(settings, "adaptive", false)) {
    if (!py::isinstance<py::bool_>(value))
      throw std::invalid_argument("Blender subdivision adaptive must be boolean");
    result.adaptive = py::cast<bool>(value);
  }
  if (auto value = field(settings, "use_creases", false)) {
    if (!py::isinstance<py::bool_>(value))
      throw std::invalid_argument("Blender subdivision use_creases must be boolean");
    result.use_creases = py::cast<bool>(value);
  }
  auto level = field(settings, "level", false);
  if (!level)
    level = field(settings, "max_level", false);
  if (level) {
    long n;
    py::detail::make_caster<long> caster;
    if (!caster.load(level, false))
      throw std::invalid_argument("Blender subdivision level must be between 0 and 16");
    n = py::detail::cast_op<long>(std::move(caster));
    if (n < 0 || n > 16)
      throw std::invalid_argument("Blender subdivision level must be between 0 and 16");
    result.max_level = int(n);
  }
  return result;
}
template <class LoopIndex>
void read_normals(MeshData &mesh, py::handle record, const Buffer &normals, LoopIndex loop_index) {
  auto domain = field(record, "normals_domain", false);
  const bool face_normals = domain && text(domain) == "FACE";
  mesh.corner_normals.reserve(mesh.triangles.size() * 3);
  for (std::size_t t = 0; t < mesh.triangles.size(); ++t) {
    Float3 ng{};
    if (face_normals) {
      const auto tri = std::as_const(mesh.triangles)[t];
      const auto &p = std::as_const(mesh.positions);
      const auto a = p[tri.a], b = p[tri.b], c = p[tri.c];
      const Float3 u{b.x - a.x, b.y - a.y, b.z - a.z}, v{c.x - a.x, c.y - a.y, c.z - a.z};
      ng = normal({u.y * v.z - u.z * v.y, u.z * v.x - u.x * v.z, u.x * v.y - u.y * v.x});
    }
    for (int c = 0; c < 3; ++c) {
      const auto i = std::size_t(loop_index(3 * t + c)) * 3;
      const Float3 n = normal({normals.number(i), normals.number(i + 1), normals.number(i + 2)});
      mesh.corner_normals.push_back(face_normals && (ng.x || ng.y || ng.z) ? ng : n);
    }
  }
}
using MeshPtr = std::shared_ptr<const MeshData>;
void destroy_mesh(void *value) {
  py::gil_scoped_release release;
  delete static_cast<MeshPtr *>(value);
}
} // namespace

std::shared_ptr<const MeshData> capture_blend_mesh(py::handle record, py::handle arrays,
                                                   const std::vector<std::string> &materials, std::uint64_t revision,
                                                   std::shared_ptr<const MeshData> previous, bool reuse_layout) {
  // The host sets reuse_layout only for matching verified content IDs of
  // every topology/attribute buffer and every field of the layout.
  auto mesh = reuse_layout && previous ? std::make_shared<MeshData>(*previous) : std::make_shared<MeshData>();
  mesh->key = text(field(record, "key"));
  mesh->revision = revision;
  mesh->materials = materials;
  if (mesh->materials.empty())
    mesh->materials.push_back("");
  auto fields = field(record, "arrays");
  Buffer positions(array(arrays, fields, "positions"), "positions", 'f', 3);
  const int points = int(positions.size / 3);
  if (reuse_layout && previous) {
    if (previous->positions.size() != std::size_t(points))
      throw std::invalid_argument("Blender pose vertex count changed");
    mesh->positions = SharedArray<Float3>{};
  }
  mesh->positions.reserve(points);
  for (int i = 0; i < points; ++i)
    mesh->positions.push_back({positions.number(3 * i), positions.number(3 * i + 1), positions.number(3 * i + 2)});
  // Blender's texture space for Generated coordinates (core/generated_coordinates.h).
  mesh->texture_space = SharedArray<Float3>{};
  if (auto space = field(record, "texture_space", false); space && !space.is_none()) {
    for (const char *name : {"location", "size"}) {
      const auto values = list(field(space, name));
      if (values.size() != 3)
        throw std::invalid_argument("Blender texture space needs three values");
      Float3 value{};
      float *out[]{&value.x, &value.y, &value.z};
      for (int i = 0; i < 3; ++i) {
        if (!py::isinstance<py::float_>(values[i]) && !py::isinstance<py::int_>(values[i]))
          throw std::invalid_argument("Blender texture space values must be numbers");
        *out[i] = py::cast<float>(values[i]);
        if (!std::isfinite(*out[i]))
          throw std::invalid_argument("Blender texture space values must be finite");
      }
      mesh->texture_space.push_back(value);
    }
  }
  // Blender's vertex normals and edges() for Pointiness and Random per Island, as Blender's
  // translator uses them (v5.3.0 intern/cycles/blender/mesh.cpp; core/mesh_pointiness.h,
  // core/mesh_islands.h). The normals follow the pose, the edges are topology. A record
  // without vertex normals keeps the face-derived rules.
  mesh->blender_vertex_normals = SharedArray<Float3>{};
  if (field(fields, "vertex_normals", false)) {
    Buffer normals(array(arrays, fields, "vertex_normals"), "vertex_normals", 'f', 3);
    normals.count(std::size_t(points) * 3);
    mesh->blender_vertex_normals.reserve(points);
    for (int i = 0; i < points; ++i)
      mesh->blender_vertex_normals.push_back(
          {normals.number(3 * i), normals.number(3 * i + 1), normals.number(3 * i + 2)});
  }
  const auto read_edges = [&]() {
    mesh->blender_edges = SharedArray<int>{};
    if (mesh->blender_vertex_normals.empty())
      return;
    Buffer edges(array(arrays, fields, "edges"), "edges", 'i', 2);
    mesh->blender_edges.reserve(edges.size);
    for (std::size_t i = 0; i < edges.size; ++i)
      mesh->blender_edges.push_back(integer(edges, i, points, "edge vertex"));
  };
  if (reuse_layout && previous) {
    if (mesh->blender_vertex_normals.empty() != previous->blender_vertex_normals.empty())
      read_edges();
    if (!mesh->subdivision) {
      Buffer loops(array(arrays, fields, "triangle_loops"), "triangle_loops", 'i', 3);
      Buffer normals(array(arrays, fields, "normals"), "normals", 'f', 3);
      loops.count(mesh->triangles.size() * 3);
      mesh->corner_normals = SharedArray<Float3>{};
      read_normals(*mesh, record, normals,
                   [&](std::size_t i) { return integer(loops, i, int(normals.size / 3), "normal loop"); });
    }
    return mesh;
  }
  Buffer counts(array(arrays, fields, "polygon_counts"), "polygon_counts", 'i', 1);
  Buffer indices(array(arrays, fields, "vertex_indices"), "vertex_indices", 'i', 1);
  Buffer slots(array(arrays, fields, "material_indices"), "material_indices", 'i', 1);
  Buffer smooth(array(arrays, fields, "smooth"), "smooth", 'B', 1);
  const int faces = int(counts.size), corners = int(indices.size);
  slots.count(faces);
  smooth.count(faces);
  // Like Blender's create_mesh (intern/cycles/blender/mesh.cpp, clamp_material_index): a
  // material index outside the slots uses the nearest slot instead of rejecting the mesh.
  const auto slot = [&](int face) {
    return std::clamp(slots.at<std::int32_t>(face), 0, int(mesh->materials.size()) - 1);
  };
  std::vector<int> corner_points, corner_faces;
  corner_points.reserve(corners);
  corner_faces.reserve(corners);
  std::size_t offset = 0;
  for (int face = 0; face < faces; ++face) {
    const int n = counts.at<std::int32_t>(face);
    if (n < 3 || std::size_t(n) > indices.size - offset)
      throw std::invalid_argument("Invalid Blender polygon corner count");
    for (int c = 0; c < n; ++c) {
      corner_points.push_back(integer(indices, offset + c, points, "vertex"));
      corner_faces.push_back(face);
    }
    offset += n;
  }
  if (offset != indices.size)
    throw std::invalid_argument("Blender polygon corners do not cover the loop array");
  read_edges();
  const auto settings = subdivision(record);
  const bool subdivided = settings.type != SubdivisionType::None;
  std::vector<int> emitted_loops, emitted_faces;
  if (subdivided) {
    auto &subd = mesh->subdivision.emplace();
    subd.settings = settings;
    subd.face_sizes.reserve(faces);
    subd.face_corners.reserve(corners);
    for (int i = 0; i < faces; ++i) {
      subd.face_sizes.push_back(counts.at<std::int32_t>(i));
      subd.face_material.push_back(slot(i));
      subd.face_provenance.push_back({i, 0});
    }
    for (int i : corner_points)
      subd.face_corners.push_back(i);
    emitted_loops.resize(corners);
    std::iota(emitted_loops.begin(), emitted_loops.end(), 0);
    emitted_faces = corner_faces;
    mesh->smooth = true;
  } else {
    Buffer triangles(array(arrays, fields, "triangle_vertices"), "triangle_vertices", 'i', 3);
    Buffer loops(array(arrays, fields, "triangle_loops"), "triangle_loops", 'i', 3);
    Buffer polygons(array(arrays, fields, "triangle_polygons"), "triangle_polygons", 'i', 1);
    loops.count(triangles.size);
    polygons.count(triangles.size / 3);
    mesh->triangles.reserve(triangles.size / 3);
    emitted_loops.reserve(triangles.size);
    emitted_faces.reserve(triangles.size);
    // Blender and Cycles both use counterclockwise front faces. The GU
    // winding reversal used by the legacy file/SOP route does not apply.
    for (std::size_t t = 0; t < polygons.size; ++t) {
      const int face = integer(polygons, t, faces, "triangle polygon");
      int vertices[3];
      for (int c = 0; c < 3; ++c) {
        const auto source = 3 * t + c;
        const int loop = integer(loops, source, corners, "triangle loop");
        vertices[c] = integer(triangles, source, points, "triangle vertex");
        if (corner_points[loop] != vertices[c] || corner_faces[loop] != face)
          throw std::invalid_argument("Blender triangle topology disagrees with polygon loops");
        emitted_loops.push_back(loop);
        emitted_faces.push_back(face);
      }
      mesh->triangles.push_back({vertices[0], vertices[1], vertices[2]});
      mesh->triangle_material.push_back(slot(face));
    }
    // Corner N includes Blender's split/flat/custom shading. A smooth mesh
    // with explicit corner normals preserves mixed polygon smooth flags.
    mesh->smooth = true;
    Buffer normals(array(arrays, fields, "normals"), "normals", 'f', 3);
    normals.count(std::size_t(corners) * 3);
    read_normals(*mesh, record, normals, [&](std::size_t i) { return emitted_loops[i]; });
  }
  std::set<std::string> reserved{"P", "N", "uv", "shop_materialpath", "creaseweight", "cornerweight"};
  const auto read_attribute = [&](py::handle info, const std::string &name, int width, char kind, int first = 0,
                                  int components = 0) {
    const auto domain = text(field(info, "domain"));
    if (domain == "EDGE")
      return;
    const auto count = domain == "POINT" ? points : domain == "CORNER" ? corners : domain == "FACE" ? faces : -1;
    if (count < 0)
      throw std::invalid_argument("Unsupported Blender attribute domain: " + domain);
    Buffer values(array(arrays, field(info, "array")), name, kind, width);
    values.count(std::size_t(count) * width);
    NamedAttribute attribute;
    attribute.name = name;
    attribute.components = components ? components : width;
    attribute.values.reserve(emitted_loops.size());
    for (std::size_t c = 0; c < emitted_loops.size(); ++c) {
      const int loop = emitted_loops[c];
      const auto source = std::size_t(domain == "POINT"    ? corner_points[loop]
                                      : domain == "CORNER" ? loop
                                                           : emitted_faces[c]) *
                              width +
                          first;
      Float3 v{};
      v.x = values.number(source);
      if (attribute.components > 1)
        v.y = values.number(source + 1);
      if (attribute.components > 2)
        v.z = values.number(source + 2);
      attribute.values.push_back(v);
    }
    mesh->attributes.push_back(std::move(attribute));
  };
  // UV sets follow the exporter's decision (blend_export.uv_sets): the default fills the
  // standard UV stream; a set is also a named field unless keep_name is false (the
  // default UV set that no material names). Records without keep_name keep every name.
  auto default_uv = field(record, "default_uv", false);
  const auto is_default_uv = [&](py::handle info) {
    return default_uv && !default_uv.is_none() && text(default_uv) == text(field(info, "name"));
  };
  for (auto layer : list(field(record, "uv_layers", false))) {
    const auto name = named(layer);
    Buffer values(array(arrays, field(layer, "array")), name, 'f', 2);
    values.count(std::size_t(corners) * 2);
    if (is_default_uv(layer)) {
      mesh->corner_uv.reserve(emitted_loops.size());
      for (int loop : emitted_loops)
        mesh->corner_uv.push_back({values.number(2 * loop), values.number(2 * loop + 1)});
    }
    if (flag(layer, "keep_name", true) && reserved.insert(name).second) {
      NamedAttribute attribute;
      attribute.name = name;
      // Two components, as Blender stores UV maps (Float2): the Attribute node's Fac is U.
      attribute.components = 2;
      attribute.values.reserve(emitted_loops.size());
      for (int loop : emitted_loops)
        attribute.values.push_back({values.number(2 * loop), values.number(2 * loop + 1), 0});
      mesh->attributes.push_back(std::move(attribute));
    }
  }
  auto default_color = field(record, "default_color", false);
  for (auto info : list(field(record, "color_attributes", false))) {
    const auto name = named(info);
    if (reserved.insert(name).second) {
      read_attribute(info, name, 4, 'f', 0, 3);
      read_attribute(info, "__cycles_blender_alpha_" + name, 4, 'f', 3, 1);
    }
    if (default_color && !default_color.is_none() && text(default_color) == text(field(info, "name"))) {
      read_attribute(info, "__cycles_blender_default_color", 4, 'f', 0, 3);
      read_attribute(info, "__cycles_blender_alpha___cycles_blender_default_color", 4, 'f', 3, 1);
    }
  }
  for (auto info : list(field(record, "attributes", false))) {
    const auto name = named(info), type = text(field(info, "data_type")), domain = text(field(info, "domain"));
    if (subdivided && settings.use_creases &&
        (text(field(info, "name")) == "crease_edge" || text(field(info, "name")) == "crease_vert")) {
      Buffer weights(array(arrays, field(info, "array")), name, 'f', 1);
      auto &subd = *mesh->subdivision;
      if (text(field(info, "name")) == "crease_edge") {
        if (domain != "EDGE")
          throw std::invalid_argument("Blender edge crease must use EDGE domain");
        Buffer edges(array(arrays, fields, "edges"), "edges", 'i', 2);
        weights.count(edges.size / 2);
        for (std::size_t i = 0; i < weights.size; ++i) {
          const auto w = weights.number(i);
          if (w < 0 || w > 1)
            throw std::invalid_argument("Blender crease weights must be between zero and one");
          if (w > 0)
            subd.edge_creases.push_back({integer(edges, 2 * i, points, "edge vertex"),
                                         integer(edges, 2 * i + 1, points, "edge vertex"), w * 10});
        }
      } else {
        if (domain != "POINT")
          throw std::invalid_argument("Blender vertex crease must use POINT domain");
        weights.count(points);
        for (int i = 0; i < points; ++i) {
          const auto w = weights.number(i);
          if (w < 0 || w > 1)
            throw std::invalid_argument("Blender crease weights must be between zero and one");
          if (w > 0)
            subd.vertex_creases.push_back({i, w * 10});
        }
      }
    }
    if (flag(info, "uv_set", false)) {
      // A generic attribute the exporter detected as a UV set (Point or Face Corner,
      // FLOAT2 or FLOAT_VECTOR); Point values are read per corner like any field.
      if (domain == "EDGE" || (type != "FLOAT2" && type != "FLOAT_VECTOR"))
        throw std::invalid_argument("Blender UV attribute must be a Point or Face Corner FLOAT2/FLOAT_VECTOR");
      const bool is_default = is_default_uv(info), keep = flag(info, "keep_name", true);
      if (is_default && !mesh->corner_uv.empty())
        throw std::invalid_argument("Blender mesh names two default UV sets");
      const bool named_field = keep && reserved.insert(name).second;
      if (!is_default && !named_field)
        continue;
      read_attribute(info, name, type == "FLOAT_VECTOR" ? 3 : 2, 'f');
      if (is_default) {
        mesh->corner_uv.reserve(emitted_loops.size());
        for (const auto &value : mesh->attributes.back().values)
          mesh->corner_uv.push_back({value.x, value.y});
        if (!named_field)
          mesh->attributes.pop_back();
      }
      continue;
    }
    if (!reserved.insert(name).second || domain == "EDGE")
      continue;
    const int width = type == "FLOAT_VECTOR" ? 3 : type == "FLOAT2" ? 2 : 1;
    const char kind = type == "BOOLEAN" ? 'B' : type == "INT" || type == "INT8" ? 'i' : 'f';
    if (type != "FLOAT" && type != "FLOAT_VECTOR" && type != "FLOAT2" && type != "INT" && type != "INT8" &&
        type != "BOOLEAN")
      throw std::invalid_argument("Unsupported Blender attribute type: " + type);
    read_attribute(info, name, width, kind);
  }
  // Cd is the renderer's established native color channel. Its ordinary
  // named-field encoding is deliberately bypassed by the shader translator.
  for (auto it = mesh->attributes.begin(); it != mesh->attributes.end(); ++it)
    if (it->name == "Cd") {
      if (it->components != 3)
        throw std::invalid_argument("Blender Cd must have three color components");
      mesh->corner_colors = std::move(it->values);
      mesh->attributes.erase(it);
      break;
    }
  if (previous) {
    // Exact validated equality is authoritative. Content IDs only select a
    // candidate; they never permit reusing incompatible topology or UVs.
    const auto share = [](auto &target, const auto &source) {
      if (target == source)
        target = source;
    };
    share(mesh->triangles, previous->triangles);
    share(mesh->positions, previous->positions);
    share(mesh->corner_normals, previous->corner_normals);
    share(mesh->corner_uv, previous->corner_uv);
    share(mesh->corner_colors, previous->corner_colors);
    share(mesh->blender_edges, previous->blender_edges);
    share(mesh->blender_vertex_normals, previous->blender_vertex_normals);
    if (mesh->subdivision && previous->subdivision) {
      auto &target = *mesh->subdivision;
      const auto &source = *previous->subdivision;
      share(target.face_sizes, source.face_sizes);
      share(target.face_corners, source.face_corners);
      share(target.face_provenance, source.face_provenance);
      share(target.edge_creases, source.edge_creases);
      share(target.vertex_creases, source.vertex_creases);
    }
    for (auto &attribute : mesh->attributes)
      for (const auto &old : previous->attributes)
        if (attribute.name == old.name && attribute.components == old.components) {
          share(attribute.values, old.values);
          break;
        }
  }
  return mesh;
}
std::shared_ptr<const MeshData> capture_blend_curves(py::handle record, py::handle arrays,
                                                     const std::vector<std::string> &materials,
                                                     std::uint64_t revision) {
  // Link hair (blend_export._hair_curves_record / _particle_hair_record, prepared per key by
  // blender_assets._native_curves): flat, already Blender-ordered arrays. Curves with fewer
  // than two keys were removed on the host (Cycles builds no segment for them).
  auto curves = std::make_shared<CurveData>();
  curves->key = text(field(record, "key"));
  curves->revision = revision;
  curves->materials = materials;
  if (curves->materials.empty())
    curves->materials.push_back("");
  auto fields = field(record, "arrays");
  Buffer positions(array(arrays, fields, "positions"), "positions", 'f', 3);
  const auto points = positions.size / 3;
  curves->positions.reserve(points);
  for (std::size_t i = 0; i < points; ++i)
    curves->positions.push_back({positions.number(3 * i), positions.number(3 * i + 1), positions.number(3 * i + 2)});
  Buffer radii(array(arrays, fields, "radii"), "radii", 'f', 1);
  radii.count(points);
  curves->radii.reserve(points);
  for (std::size_t i = 0; i < points; ++i) {
    const auto radius = radii.number(i);
    if (radius < 0)
      throw std::invalid_argument("Blender curve radius must be nonnegative");
    curves->radii.push_back(radius);
  }
  Buffer first(array(arrays, fields, "first_key"), "first_key", 'i', 1);
  const auto count = first.size;
  curves->first_key.reserve(count);
  for (std::size_t i = 0; i < count; ++i) {
    const auto key = first.at<std::int32_t>(i);
    const auto end = i + 1 < count ? std::int64_t(first.at<std::int32_t>(i + 1)) : std::int64_t(points);
    if ((i == 0 && key != 0) || key < 0 || end - key < 2 || end > std::int64_t(points))
      throw std::invalid_argument("Blender curves need ordered curves of at least two keys");
    curves->first_key.push_back(key);
  }
  if (!count && points)
    throw std::invalid_argument("Blender curve keys without curves");
  Buffer randoms(array(arrays, fields, "randoms"), "randoms", 'f', 1);
  randoms.count(count);
  curves->randoms.reserve(count);
  for (std::size_t i = 0; i < count; ++i)
    curves->randoms.push_back(std::clamp(randoms.number(i), 0.0f, 1.0f));
  if (field(fields, "uv", false)) {
    Buffer uv(array(arrays, fields, "uv"), "uv", 'f', 2);
    uv.count(count * 2);
    curves->uv.reserve(count);
    for (std::size_t i = 0; i < count; ++i)
      curves->uv.push_back({uv.number(2 * i), uv.number(2 * i + 1)});
  }
  curves->generated_attribute = flag(record, "generated_attribute", true);
  if (field(fields, "generated", false)) {
    Buffer generated(array(arrays, fields, "generated"), "generated", 'f', 3);
    generated.count(count * 3);
    curves->generated.reserve(count);
    for (std::size_t i = 0; i < count; ++i)
      curves->generated.push_back({generated.number(3 * i), generated.number(3 * i + 1), generated.number(3 * i + 2)});
    curves->generated_attribute = true;
  }
  if (field(fields, "material_slots", false)) {
    Buffer slots(array(arrays, fields, "material_slots"), "material_slots", 'i', 1);
    slots.count(count);
    curves->material_slots.reserve(count);
    for (std::size_t i = 0; i < count; ++i)
      curves->material_slots.push_back(integer(slots, i, int(curves->materials.size()), "curve material slot"));
  }
  const auto shape = text(field(record, "shape"));
  if (shape == "LINEAR")
    curves->shape = CurveShape::Linear;
  else if (shape == "THICK")
    curves->shape = CurveShape::Thick;
  else if (shape == "RIBBON")
    curves->shape = CurveShape::Ribbon;
  else
    throw std::invalid_argument("Unsupported Blender curve shape: " + shape);
  std::set<std::string> names;
  for (auto info : list(field(record, "attributes", false))) {
    const auto name = named(info);
    const auto width = py::cast<int>(field(info, "components"));
    if (width < 1 || width > 3)
      throw std::invalid_argument("Blender curve attribute needs one to three components: " + name);
    if (!names.insert(name).second)
      continue;
    Buffer values(array(arrays, field(info, "array")), name, 'f', width);
    values.count(points * std::size_t(width));
    NamedAttribute attribute;
    attribute.name = name;
    attribute.components = width;
    attribute.values.reserve(points);
    for (std::size_t i = 0; i < points; ++i) {
      Float3 v{};
      v.x = values.number(i * width);
      if (width > 1)
        v.y = values.number(i * width + 1);
      if (width > 2)
        v.z = values.number(i * width + 2);
      attribute.values.push_back(v);
    }
    curves->attributes.push_back(std::move(attribute));
  }
  auto mesh = std::make_shared<MeshData>();
  mesh->key = curves->key;
  mesh->revision = revision;
  mesh->materials = curves->materials;
  mesh->external_curves = std::move(curves);
  return mesh;
}

std::shared_ptr<const MeshData> capture_blend_points(py::handle record, py::handle arrays,
                                                     const std::vector<std::string> &materials,
                                                     std::uint64_t revision) {
  // Link point clouds (blend_export._point_cloud_record, prepared per key by
  // blender_assets._native_points): Blender's points in their own order. Blender's translator
  // (v5.3.0 intern/cycles/blender/pointcloud.cpp export_pointcloud) gives every point shader 0
  // and no Generated attribute.
  auto points = std::make_shared<PointData>();
  points->key = text(field(record, "key"));
  points->revision = revision;
  points->materials = materials;
  if (points->materials.empty())
    points->materials.push_back("");
  points->generated_attribute = flag(record, "generated_attribute", false);
  auto fields = field(record, "arrays");
  Buffer positions(array(arrays, fields, "positions"), "positions", 'f', 3);
  const auto count = positions.size / 3;
  points->positions.reserve(count);
  for (std::size_t i = 0; i < count; ++i)
    points->positions.push_back({positions.number(3 * i), positions.number(3 * i + 1), positions.number(3 * i + 2)});
  Buffer radii(array(arrays, fields, "radii"), "radii", 'f', 1);
  radii.count(count);
  points->radii.reserve(count);
  for (std::size_t i = 0; i < count; ++i) {
    const auto radius = radii.number(i);
    if (!std::isfinite(radius) || radius < 0)
      throw std::invalid_argument("Blender point radius must be finite and nonnegative");
    points->radii.push_back(radius);
  }
  if (field(fields, "randoms", false)) {
    Buffer randoms(array(arrays, fields, "randoms"), "randoms", 'f', 1);
    randoms.count(count);
    points->randoms.reserve(count);
    for (std::size_t i = 0; i < count; ++i)
      points->randoms.push_back(std::clamp(randoms.number(i), 0.0f, 1.0f));
  }
  std::set<std::string> names;
  for (auto info : list(field(record, "attributes", false))) {
    const auto name = named(info);
    const auto width = py::cast<int>(field(info, "components"));
    if (width < 1 || width > 3)
      throw std::invalid_argument("Blender point attribute needs one to three components: " + name);
    if (!names.insert(name).second)
      continue;
    Buffer values(array(arrays, field(info, "array")), name, 'f', width);
    values.count(count * std::size_t(width));
    NamedAttribute attribute;
    attribute.name = name;
    attribute.components = width;
    attribute.values.reserve(count);
    for (std::size_t i = 0; i < count; ++i) {
      Float3 v{};
      v.x = values.number(i * width);
      if (width > 1)
        v.y = values.number(i * width + 1);
      if (width > 2)
        v.z = values.number(i * width + 2);
      attribute.values.push_back(v);
    }
    points->attributes.push_back(std::move(attribute));
  }
  auto mesh = std::make_shared<MeshData>();
  mesh->key = points->key;
  mesh->revision = revision;
  mesh->materials = points->materials;
  mesh->external_points = std::move(points);
  return mesh;
}

std::vector<double> blend_mesh_bounds(const MeshData &mesh) {
  // Link hair and point clouds: every key's or point's sphere (Cycles bounds them by position
  // and radius).
  const auto *curves = mesh.external_curves.get();
  const auto *points = mesh.external_points.get();
  const auto *spheres = curves ? &curves->positions : points ? &points->positions : nullptr;
  const auto *radii = curves ? &curves->radii : points ? &points->radii : nullptr;
  if (spheres ? spheres->empty() : mesh.positions.empty())
    return {};
  std::vector<double> box{INFINITY, INFINITY, INFINITY, -INFINITY, -INFINITY, -INFINITY};
  const auto count = spheres ? spheres->size() : mesh.positions.size();
  for (std::size_t index = 0; index < count; ++index) {
    const auto &point = spheres ? (*spheres)[index] : mesh.positions[index];
    const double radius = radii ? (*radii)[index] : 0.0;
    const double values[]{point.x, point.y, point.z};
    for (int i = 0; i < 3; ++i) {
      if (!std::isfinite(values[i]))
        throw std::invalid_argument("Non-finite Blender mesh bounds");
      box[i] = std::min(box[i], values[i] - radius);
      box[i + 3] = std::max(box[i + 3], values[i] + radius);
    }
  }
  return box;
}

std::vector<std::pair<std::uintptr_t, std::size_t>> blend_mesh_storage(const MeshData &mesh) {
  std::vector<std::pair<std::uintptr_t, std::size_t>> result;
  const auto add = [&](const void *identity, std::size_t bytes) {
    if (identity && bytes)
      result.emplace_back(reinterpret_cast<std::uintptr_t>(identity), bytes);
  };
  const auto shared = [&](const auto &values) {
    add(values.storage_identity(), values.capacity() * sizeof(typename std::decay_t<decltype(values)>::value_type));
  };
  const auto vector = [&](const auto &values) {
    add(values.data(), values.capacity() * sizeof(typename std::decay_t<decltype(values)>::value_type));
  };
  add(&mesh, sizeof(mesh) + mesh.key.capacity());
  if (const auto *curves = mesh.external_curves.get()) {
    add(curves, sizeof(*curves) + curves->key.capacity());
    vector(curves->positions);
    vector(curves->radii);
    vector(curves->first_key);
    vector(curves->randoms);
    vector(curves->uv);
    vector(curves->generated);
    vector(curves->material_slots);
    vector(curves->attributes);
    for (const auto &attribute : curves->attributes) {
      add(attribute.name.data(), attribute.name.capacity());
      shared(attribute.values);
    }
  }
  if (const auto *points = mesh.external_points.get()) {
    add(points, sizeof(*points) + points->key.capacity());
    vector(points->positions);
    vector(points->radii);
    vector(points->randoms);
    vector(points->attributes);
    for (const auto &attribute : points->attributes) {
      add(attribute.name.data(), attribute.name.capacity());
      shared(attribute.values);
    }
  }
  shared(mesh.positions);
  shared(mesh.triangles);
  shared(mesh.corner_normals);
  shared(mesh.corner_uv);
  shared(mesh.corner_colors);
  shared(mesh.pointiness);
  shared(mesh.texture_space);
  shared(mesh.blender_edges);
  shared(mesh.blender_vertex_normals);
  vector(mesh.materials);
  for (const auto &value : mesh.materials)
    add(value.data(), value.capacity());
  vector(mesh.triangle_material);
  vector(mesh.attributes);
  for (const auto &attribute : mesh.attributes) {
    add(attribute.name.data(), attribute.name.capacity());
    shared(attribute.values);
    shared(attribute.motion_values);
    for (const auto &values : attribute.motion_values)
      vector(values);
  }
  shared(mesh.motion.positions);
  for (const auto &values : mesh.motion.positions)
    vector(values);
  shared(mesh.motion.radii);
  for (const auto &values : mesh.motion.radii)
    vector(values);
  shared(mesh.motion_corner_normals);
  for (const auto &values : mesh.motion_corner_normals)
    vector(values);
  shared(mesh.motion_corner_uv);
  for (const auto &values : mesh.motion_corner_uv)
    vector(values);
  shared(mesh.motion_corner_colors);
  for (const auto &values : mesh.motion_corner_colors)
    vector(values);
  if (mesh.subdivision) {
    const auto &value = *mesh.subdivision;
    shared(value.face_sizes);
    shared(value.face_corners);
    vector(value.face_material);
    shared(value.face_provenance);
    shared(value.edge_creases);
    shared(value.vertex_creases);
  }
  if (mesh.nominal_pose) {
    const auto &value = *mesh.nominal_pose;
    shared(value.positions);
    shared(value.corner_normals);
    shared(value.corner_uv);
    shared(value.corner_colors);
    vector(value.attributes);
    for (const auto &attribute : value.attributes) {
      add(attribute.name.data(), attribute.name.capacity());
      shared(attribute.values);
    }
  }
  return result;
}
std::size_t blend_mesh_nbytes(const MeshData &mesh) {
  std::size_t result = 0;
  std::set<std::uintptr_t> counted;
  for (const auto &[identity, bytes] : blend_mesh_storage(mesh))
    if (counted.insert(identity).second)
      result += bytes;
  return result;
}
py::capsule blend_mesh_capsule(std::shared_ptr<const MeshData> mesh) {
  auto value = std::make_unique<MeshPtr>(std::move(mesh));
  py::capsule result(value.get(), "cycles_houdini.BlendMesh", destroy_mesh);
  value.release();
  return result;
}
std::shared_ptr<const MeshData> blend_mesh_from_capsule(py::handle value) {
  if (!value || !py::isinstance<py::capsule>(value))
    throw std::invalid_argument("Invalid immutable Blender mesh capsule");
  auto capsule = py::reinterpret_borrow<py::capsule>(value);
  if (!capsule.name() || std::strcmp(capsule.name(), "cycles_houdini.BlendMesh") != 0)
    throw std::invalid_argument("Invalid immutable Blender mesh capsule");
  auto *pointer = static_cast<MeshPtr *>(capsule.get_pointer());
  if (!pointer)
    throw std::invalid_argument("Invalid immutable Blender mesh capsule");
  return *pointer;
}
} // namespace cycles_houdini
