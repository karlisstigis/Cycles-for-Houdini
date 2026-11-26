// SPDX-License-Identifier: Apache-2.0
#include "subdivision_shading_contracts.h"
#include "../core/subdivision_mesh.h"
#include "subd/dice.h"
#include "subd/interpolation.h"
#include "subd/subpatch.h"
#include "util/tbb.h"
#include "scene/shader_nodes.h"
#include "scene/object.h"
#include <iostream>
#include <numeric>

namespace cycles_houdini::harness {
void subdivision_shading_contracts() {
  int checks = 0;
  const auto require = [&](bool value, const char *message) {
    ++checks;
    if (!value)
      throw std::runtime_error(message);
  };
  int count_overflow_checks = 0;
  {
    // Exercise the real patched count functions without allocating a
    // tessellation. High edge factors cross signed 32-bit products and
    // final counts while the independent oracle stays in 64-bit math.
    ccl::SubEdge edges[4];
    ccl::SubPatch patch(nullptr, 0);
    for (int i = 0; i < 4; ++i)
      patch.edges[i].edge = &edges[i];
    const auto count_require = [&](bool ok, const char *message) {
      ++count_overflow_checks;
      require(ok, message);
    };
    // 92682 conservatively covers a unit-square UV diagonal at the
    // integration's maximum subdivision level 16 (axis factor 65536).
    for (const int factor : {1, 2, 3, 32768, 46341, 65536, 92682}) {
      for (auto &edge : edges)
        edge.T = factor;
      const std::int64_t n = factor;
      patch.shape = ccl::SubPatch::QUAD;
      count_require(patch.calc_num_inner_verts() == (n - 1) * (n - 1), "Quad subdivision vertex count overflowed");
      count_require(patch.calc_num_triangles() == 2 * n * n, "Quad subdivision triangle count overflowed");
      patch.shape = ccl::SubPatch::TRIANGLE;
      const std::int64_t inner_vertices = n <= 2 ? 0 : n * (n - 1) / 2;
      count_require(patch.calc_num_inner_verts() == inner_vertices, "Triangle subdivision vertex count overflowed");
      // Euler's formula for a triangulated disk: F = 2I + B - 2,
      // with three boundary edges of n segments. This count is not n*n
      // for the SubPatch interior-grid convention when n > 2.
      count_require(patch.calc_num_triangles() == 2 * inner_vertices + 3 * n - 2,
                    "Triangle subdivision triangle count overflowed");
    }
    patch.shape = ccl::SubPatch::QUAD;
    edges[0].T = edges[2].T = 1;
    edges[1].T = edges[3].T = 65536;
    count_require(patch.calc_num_inner_verts() == 0 && patch.calc_num_triangles() == 131072,
                  "Thin quad U count is incorrect");
    edges[0].T = edges[2].T = 65536;
    edges[1].T = edges[3].T = 1;
    count_require(patch.calc_num_inner_verts() == 0 && patch.calc_num_triangles() == 131072,
                  "Thin quad V count is incorrect");
    edges[0].T = 32768;
    edges[2].T = 65536;
    edges[1].T = 65536;
    edges[3].T = 46341;
    count_require(patch.calc_num_inner_verts() == 4294836225LL, "Asymmetric quad vertex count lost the maximum factor");
    count_require(patch.calc_num_triangles() == 8589882629LL, "Asymmetric quad triangle count overflowed");
  }
  {
    // Construct real nodes: both generic and generated socket setters must
    // preserve authored bits and report only actual value changes.
    ccl::ValueNode value, other;
    for (const std::uint32_t bits : {0u, 0x80000000u, 1u, 0x80000001u, 0x3f800000u, 0xbf800000u}) {
      float authored;
      std::memcpy(&authored, &bits, sizeof(bits));
      value.set_value(authored);
      require(std::bit_cast<std::uint32_t>(value.get_value()) == bits, "Engine scalar setter discarded authored bits");
      value.clear_modified();
      value.set_value(authored);
      require(!value.value_is_modified(), "Identical scalar socket bits caused another update");
      other.set(*other.get_value_socket(), authored);
      require(value.equals_value(other, *value.get_value_socket()),
              "Equal socket values differ between generic/generated setters");
      const std::uint32_t opposite = bits ^ 0x80000000u;
      float changed;
      std::memcpy(&changed, &opposite, sizeof(changed));
      other.set_value(changed);
      require(!value.equals_value(other, *value.get_value_socket()), "Node equality discarded different float bits");
    }
    float negative{};
    const std::uint32_t sign = 0x80000000u;
    std::memcpy(&negative, &sign, sizeof(sign));
    ccl::SeparateXYZNode vector;
    vector.set_vector(ccl::make_float3(0, 0, 0));
    vector.clear_modified();
    vector.set_vector(ccl::make_float3(negative, 0, 0));
    require(std::bit_cast<std::uint32_t>(vector.get_vector().x) == sign && vector.vector_is_modified(),
            "Engine vector setter discarded a zero sign");
    vector.clear_modified();
    auto padded = vector.get_vector();
    if constexpr (sizeof(ccl::float3) > 3 * sizeof(float)) {
      const std::uint32_t ignored = 0x3f123456u;
      std::memcpy(reinterpret_cast<char *>(&padded) + 3 * sizeof(float), &ignored, sizeof(ignored));
      vector.set_vector(padded);
      require(!vector.vector_is_modified(), "Unused float3 padding invalidated a socket");
    }
    ccl::Object object;
    object.set_dupli_uv(ccl::make_float2(negative, 0));
    require(std::bit_cast<std::uint32_t>(object.get_dupli_uv().x) == sign,
            "Engine float2 setter discarded authored bits");
    auto matrix = ccl::transform_identity();
    matrix.x.y = negative;
    object.set(*object.get_tfm_socket(), matrix);
    require(std::bit_cast<std::uint32_t>(object.get_tfm().x.y) == sign,
            "Engine transform setter discarded authored bits");
  }
  const auto snapshot = [](const ccl::Mesh &mesh) {
    std::string result;
    const auto bytes = [&](const void *data, std::size_t count) {
      result.append(static_cast<const char *>(data), count);
    };
    bytes(mesh.get_triangles().data(), mesh.get_triangles().size() * sizeof(int));
    bytes(mesh.get_shader().data(), mesh.get_shader().size() * sizeof(int));
    bytes(mesh.get_smooth().data(), mesh.get_smooth().size() * sizeof(bool));
    for (const auto standard : {ccl::ATTR_STD_POSITION, ccl::ATTR_STD_VERTEX_NORMAL, ccl::ATTR_STD_CORNER_NORMAL})
      if (const auto *attr = mesh.attributes.find(standard))
        for (int step = 0; step < attr->num_motion_steps(); ++step)
          bytes(attr->data(step), attr->size * attr->data_sizeof());
    return result;
  };
  const auto clear = [](ccl::Mesh &mesh) {
    mesh.clear_modified();
    mesh.attributes.clear_modified();
    mesh.subd_attributes.clear_modified();
  };
  for (const auto type : {SubdivisionType::Linear, SubdivisionType::CatmullClark}) {
    MeshData source;
    source.key = "shading_contract";
    source.positions = {{0, 0, 0}, {1, 0, 0}, {1, 1, 0}, {0, 1, 0}};
    source.subdivision.emplace();
    auto &subd = *source.subdivision;
    subd.settings.type = type;
    subd.settings.adaptive = false;
    subd.settings.space = SubdivisionSpace::Object;
    subd.settings.max_level = 3;
    subd.face_sizes = {4};
    subd.face_corners = {0, 1, 2, 3};
    source.corner_uv = {{0, 0}, {1, 0}, {1, 1}, {0, 1}};
    source.corner_colors = {{0, 0, .5f}, {1, 0, .5f}, {1, 1, .5f}, {0, 1, .5f}};
    source.motion.positions = {source.positions, source.positions};
    for (auto &p : source.motion.positions[0])
      p.z = -.1f;
    for (auto &p : source.motion.positions[1])
      p.z = .1f;
    NamedAttribute field;
    field.name = "sample";
    field.components = 1;
    for (const auto p : source.positions)
      field.values.push_back({p.x + 2 * p.y, 0, 0});
    field.motion_values = {field.values, field.values};
    source.attributes = {field};
    auto classified = source;
    classified.corner_colors.assign(4, {.2f, .3f, .4f});
    require(same_mesh_geometry(source, classified), "Independent color fields changed geometric correspondence");
    classified = source;
    classified.corner_normals.assign(4, {0, 0, 1});
    require(!same_mesh_geometry(source, classified), "An authored normal edit was classified as shading-only");
    classified = source;
    classified.positions[0].x += .1f;
    require(!same_mesh_geometry(source, classified), "A position edit was classified as shading-only");
    classified = source;
    classified.subdivision->face_corners = {0, 2, 1, 3};
    require(!same_mesh_geometry(source, classified), "A connectivity edit was classified as shading-only");
    DisplacementFields used;
    used.named.insert("sample");
    classified = source;
    classified.corner_colors.assign(4, {.2f, .3f, .4f});
    require(same_displacement_fields(source, classified, used), "An unused color field invalidated displacement");
    classified.attributes[0].values[0].x += .1f;
    require(!same_displacement_fields(source, classified, used), "An actual height attribute edit reused displacement");
    // Numeric equality is insufficient for shader hashes. Every used
    // field, including shutter and requested-frame recovery data, retains
    // all authored bits; irrelevant fields still keep completed geometry.
    auto bit_base = source;
    bit_base.corner_normals.assign(4, {0, 0, 1});
    bit_base.pointiness.assign(4, 0.f);
    bit_base.motion_corner_normals = {static_cast<const std::vector<Float3> &>(bit_base.corner_normals),
                                      static_cast<const std::vector<Float3> &>(bit_base.corner_normals)};
    bit_base.motion_corner_uv = {static_cast<const std::vector<Float2> &>(bit_base.corner_uv),
                                 static_cast<const std::vector<Float2> &>(bit_base.corner_uv)};
    bit_base.motion_corner_colors = {static_cast<const std::vector<Float3> &>(bit_base.corner_colors),
                                     static_cast<const std::vector<Float3> &>(bit_base.corner_colors)};
    bit_base.nominal_pose = MeshNominalPose{bit_base.positions, bit_base.corner_normals, bit_base.corner_uv,
                                            bit_base.corner_colors, bit_base.attributes};
    const auto negative_zero = [](float &value) {
      const std::uint32_t bits = 0x80000000u;
      std::memcpy(&value, &bits, sizeof(bits));
    };
    const auto used_bits = [&](auto mutate, DisplacementFields demand) {
      auto next = bit_base;
      mutate(next);
      require(!same_displacement_fields(bit_base, next, demand),
              "A bit-sensitive height field edit reused displacement");
      require(same_displacement_fields(bit_base, next, {}), "An unused bit-sensitive field invalidated displacement");
      require(same_mesh_geometry(bit_base, next), "Independent field bits changed geometric correspondence");
    };
    DisplacementFields named_bits;
    named_bits.named.insert("sample");
    used_bits([&](auto &m) { negative_zero(m.attributes[0].values[0].x); }, named_bits);
    used_bits([&](auto &m) { negative_zero(m.attributes[0].motion_values[0][0].x); }, named_bits);
    used_bits([&](auto &m) { negative_zero(m.nominal_pose->attributes[0].values[0].x); }, named_bits);
    DisplacementFields uv_bits;
    uv_bits.uv = true;
    used_bits([&](auto &m) { negative_zero(m.corner_uv[0].x); }, uv_bits);
    used_bits([&](auto &m) { negative_zero(m.motion_corner_uv[0][0].x); }, uv_bits);
    used_bits([&](auto &m) { negative_zero(m.nominal_pose->corner_uv[0].x); }, uv_bits);
    DisplacementFields color_bits;
    color_bits.color = true;
    used_bits([&](auto &m) { negative_zero(m.corner_colors[0].x); }, color_bits);
    used_bits([&](auto &m) { negative_zero(m.motion_corner_colors[0][0].x); }, color_bits);
    used_bits([&](auto &m) { negative_zero(m.nominal_pose->corner_colors[0].x); }, color_bits);
    DisplacementFields intrinsic_bits;
    intrinsic_bits.pointiness = true;
    used_bits([&](auto &m) { negative_zero(m.pointiness[0]); }, intrinsic_bits);
    const auto shape_bits = [&](auto mutate) {
      auto next = bit_base;
      mutate(next);
      require(!same_mesh_geometry(bit_base, next), "Bit-sensitive geometry input was treated as unchanged");
    };
    shape_bits([&](auto &m) { negative_zero(m.positions[0].x); });
    shape_bits([&](auto &m) { negative_zero(m.corner_normals[0].x); });
    shape_bits([&](auto &m) { negative_zero(m.motion.positions[0][0].x); });
    shape_bits([&](auto &m) { negative_zero(m.motion_corner_normals[0][0].x); });
    shape_bits([&](auto &m) { negative_zero(m.nominal_pose->positions[0].x); });
    shape_bits([&](auto &m) { negative_zero(m.nominal_pose->corner_normals[0].x); });
    auto before_assignment = source;
    before_assignment.materials = {"left", "right"};
    classified = before_assignment;
    classified.subdivision->face_material = {1};
    require(same_displacement_assignments(before_assignment, {"height", "height"}, classified, {"height", "height"}),
            "Equal height functions became incompatible after a material assignment change");
    require(
        !same_displacement_assignments(before_assignment, {"height", "height"}, classified, {"height", "different"}),
        "Different height functions reused displaced positions");
    bool malformed_palette = false;
    try {
      same_displacement_assignments(before_assignment, {"height", "height"}, classified, {"height"});
    } catch (const std::logic_error &) {
      malformed_palette = true;
    }
    require(malformed_palette, "Invalid internal displacement palette ownership was accepted");
    ccl::Mesh mesh;
    mesh.resize_mesh(4, 0);
    mesh.set_motion_steps(3);
    mesh.set_use_motion_blur(true);
    for (int i = 0; i < 4; ++i)
      mesh.get_position_for_write()[i] =
          ccl::packed_float3(ccl::make_float3(source.positions[i].x, source.positions[i].y, 0));
    auto budget = std::make_shared<ccl::SubdivisionShadingBudget>(1024 * 1024);
    mesh.subdivision_shading_budget = budget;
    auto metrics = std::make_shared<SubdivisionMetrics>();
    std::atomic<std::uint64_t> tessellations{};
    fill_subdivision(mesh, source, false, ccl::transform_identity(), metrics, tessellations, false);
    auto *pointiness = mesh.subd_attributes.add(ccl::ATTR_STD_POINTINESS)->data_for_write<float>();
    for (int i = 0; i < 4; ++i)
      pointiness[i] = .1f;
    ccl::SubdParams params(&mesh);
    params.fixed = true;
    params.max_level = 3;
    params.triangle_limit = 100000;
    mesh.tessellate(params);
    clear(mesh);
    const auto *coarse_normals = mesh.subd_attributes.find(ccl::ATTR_STD_VERTEX_NORMAL);
    const auto *dense_normals = mesh.attributes.find(ccl::ATTR_STD_VERTEX_NORMAL);
    require((!coarse_normals || coarse_normals->size == 4) && dense_normals &&
                dense_normals->size == mesh.num_verts() && dense_normals->num_motion_steps() == 3,
            "Coarse and diced motion normals do not retain their own vertex counts");
    require(mesh.subdivision_shading_samples && mesh.subdivision_shading_samples->ready,
            "Completed dicing did not retain a ready owned sampling map");
    require(budget->used == mesh.subdivision_shading_bytes() && budget->used > 0 && budget->used <= budget->limit,
            "Sampling-map budget does not match owned storage");
    require(metrics->ready && metrics->bytes >= mesh.subdivision_shading_bytes() && tessellations == 1,
            "Native subdivision accounting omitted owned map storage");
    const auto geometry = snapshot(mesh);
    const auto triangles = mesh.get_triangles();
    auto *sample = mesh.subd_attributes.find(ccl::ustring(engine_attribute_name("sample")));
    require(sample && sample->size == 4 && sample->num_motion_steps() == 3,
            "The fixture's owned sampled scalar field has the wrong name/size/motion layout");
    for (int step = 0; step < 3; ++step) {
      auto *values = sample->data_for_write<float>(step);
      for (int i = 0; i < 4; ++i)
        values[i] = 2 * source.positions[i].x + 4 * source.positions[i].y + .25f + .5f * step;
    }
    sample->modified = true;
    {
      ccl::OsdMesh osd_mesh(mesh);
      ccl::OsdData osd_data;
      ccl::SubdAttributeInterpolation query(mesh, osd_mesh, osd_data);
      require(!query.needs_osd(true), "A scalar corner field requested an unused OpenSubdiv refiner");
      require(mesh.interpolate_subdivision_shading() && snapshot(mesh) == geometry,
              "Scalar-only interpolation changed geometry or required unavailable refinement data");
      bool scalar_ok = true;
      const auto *values = mesh.attributes.find(ccl::ustring(engine_attribute_name("sample")));
      for (int step = 0; step < 3; ++step)
        for (std::size_t corner = 0; corner < mesh.get_triangles().size(); ++corner) {
          const auto p = mesh.get_position()[mesh.get_triangles()[corner]];
          scalar_ok &= std::abs(values->data<float>(step)[corner] - (2 * p.x + 4 * p.y + .25f + .5f * step)) < 2e-5;
        }
      require(scalar_ok, "Scalar-only retained values disagree with independent affine fields");
      auto *requested_uv = mesh.subd_attributes.find(ccl::ATTR_STD_UV);
      requested_uv->modified = true;
      require(query.needs_osd(true) == (type == SubdivisionType::CatmullClark),
              "Smooth UV demand lost its required Catmull-Clark refinement data");
      requested_uv->modified = false;
    }
    auto *uv = mesh.subd_attributes.find(ccl::ATTR_STD_UV)->data_for_write<ccl::float2>();
    auto *cd = mesh.subd_attributes.find(ccl::ustring("Cd"))->data_for_write<ccl::packed_float3>();
    auto *intrinsic = mesh.subd_attributes.find(ccl::ATTR_STD_POINTINESS)->data_for_write<float>();
    auto *added = mesh.subd_attributes.add(ccl::ustring(engine_attribute_name("added")), ccl::TypeVector,
                                           ccl::ATTR_ELEMENT_CORNER);
    added->flags |= ccl::ATTR_SUBDIVIDE_SMOOTH_FVAR;
    auto *vector = added->data_for_write<ccl::packed_float3>();
    for (int i = 0; i < 4; ++i) {
      const auto p = source.positions[i];
      uv[i] = ccl::make_float2(2 * p.x, 3 * p.y);
      cd[i] = ccl::packed_float3(ccl::make_float3(.5f * p.x, .25f * p.y, .75f));
      intrinsic[i] = .1f + .1f * p.x + .2f * p.y;
      vector[i] = ccl::packed_float3(ccl::make_float3(3 * p.x, 2 * p.y, .7f));
    }
    // data_for_write() handles ownership; explicit dirty flags select which
    // coarse fields must be resampled, just as in an accepted scene update.
    for (auto *attribute :
         {sample, mesh.subd_attributes.find(ccl::ATTR_STD_UV), mesh.subd_attributes.find(ccl::ustring("Cd")),
          mesh.subd_attributes.find(ccl::ATTR_STD_POINTINESS), added}) {
      attribute->modified = true;
    }
    require(mesh.interpolate_subdivision_shading(),
            "Retained linear/Catmull-Clark sampling rejected an attribute-only update");
    require(snapshot(mesh) == geometry && mesh.get_triangles() == triangles,
            "Shading interpolation changed positions, motion normals, topology or primitive bindings");
    require(!mesh.position_is_modified() && !mesh.triangles_is_modified() && tessellations == 1,
            "Attribute interpolation marked geometric work or retessellated");
    require(mesh.attributes.find(ccl::ustring(engine_attribute_name("sample")))->modified &&
                mesh.attributes.find(ccl::ATTR_STD_UV)->modified &&
                !mesh.attributes.find(ccl::ATTR_STD_POSITION)->modified,
            "Resampled fields were not tagged for upload independently of positions");
    std::array<double, 5> errors{};
    const auto *positions = mesh.get_position();
    const auto *rendered = mesh.attributes.find(ccl::ustring(engine_attribute_name("sample")));
    const auto *rendered_uv = mesh.attributes.find(ccl::ATTR_STD_UV)->data<ccl::float2>();
    const auto *rendered_cd = mesh.attributes.find(ccl::ustring("Cd"))->data<ccl::packed_float3>();
    const auto *rendered_added =
        mesh.attributes.find(ccl::ustring(engine_attribute_name("added")))->data<ccl::packed_float3>();
    const auto *rendered_intrinsic = mesh.attributes.find(ccl::ATTR_STD_POINTINESS)->data<float>();
    for (std::size_t corner = 0; corner < mesh.get_triangles().size(); ++corner) {
      const int vertex = mesh.get_triangles()[corner];
      const auto p = ccl::float3(positions[vertex]);
      for (int step = 0; step < 3; ++step)
        errors[0] = std::max(
            errors[0], double(std::abs(rendered->data<float>(step)[corner] - (2 * p.x + 4 * p.y + .25f + .5f * step))));
      errors[1] = std::max(
          errors[1], double(std::abs(rendered_uv[corner].x - 2 * p.x) + std::abs(rendered_uv[corner].y - 3 * p.y)));
      const auto color = ccl::float3(rendered_cd[corner]), value = ccl::float3(rendered_added[corner]);
      errors[2] = std::max(
          errors[2], double(std::abs(color.x - .5f * p.x) + std::abs(color.y - .25f * p.y) + std::abs(color.z - .75f)));
      errors[3] = std::max(errors[3],
                           double(std::abs(value.x - 3 * p.x) + std::abs(value.y - 2 * p.y) + std::abs(value.z - .7f)));
      errors[4] = std::max(errors[4], double(std::abs(rendered_intrinsic[vertex] - (.1f + .1f * p.x + .2f * p.y))));
    }
    std::cerr << "errors scalar/uv/color/vector/intrinsic";
    for (const auto value : errors)
      std::cerr << ' ' << value;
    std::cerr << '\n';
    require(*std::max_element(errors.begin(), errors.end()) < 2e-5,
            "Retained scalar/vector/UV/color/intrinsic interpolation disagrees with independent affine fields");
    clear(mesh);
    auto changed = source;
    changed.corner_uv.assign(4, {.2f, .7f});
    changed.corner_colors.assign(4, {.3f, .4f, .5f});
    changed.attributes[0].values.assign(4, {.37f, 0, 0});
    changed.attributes[0].motion_values.assign(2, std::vector<Float3>(4, {.6f, 0, 0}));
    changed.attributes[0].motion_values[1].assign(4, {.8f, 0, 0});
    NamedAttribute lookalike;
    lookalike.name = "sample.tangent";
    lookalike.components = 1;
    lookalike.values.assign(4, {.9f, 0, 0});
    changed.attributes.push_back(lookalike);
    const auto unrelated_name = ccl::ustring(engine_attribute_name("unrelated") + ".tangent");
    mesh.attributes.add(unrelated_name, ccl::TypeVector, ccl::ATTR_ELEMENT_CORNER);
    const std::vector<float> islands{.33f}, curvature(4, .25f);
    require(refresh_mesh_shading(mesh, changed, true, {}, islands, curvature, true),
            "Owned field deltas rejected completed subdivision");
    require(snapshot(mesh) == geometry && tessellations == 1,
            "Field deltas changed geometry/motion or repeated dicing");
    require(!mesh.subd_attributes.find(ccl::ustring(engine_attribute_name("added"))) &&
                !mesh.attributes.find(ccl::ustring(engine_attribute_name("added"))) &&
                mesh.attributes.find(unrelated_name) &&
                mesh.attributes.find(ccl::ustring(engine_attribute_name("sample.tangent"))),
            "Managed fields collided with engine tangent names or failed to retire");
    const auto *refreshed = mesh.attributes.find(ccl::ustring(engine_attribute_name("sample")));
    bool scalar_ok = refreshed && refreshed->num_motion_steps() == 3;
    for (int step = 0; scalar_ok && step < 3; ++step)
      for (int corner = 0; corner < refreshed->size; ++corner)
        scalar_ok &= std::abs(refreshed->data<float>(step)[corner] - (step == 0 ? .37f : step == 1 ? .6f : .8f)) < 2e-6;
    require(scalar_ok, "Retained sampled scalar values differ from authored constants");
    const auto *uv_field = mesh.attributes.find(ccl::ATTR_STD_UV);
    const auto *color_field = mesh.attributes.find(ccl::ustring("Cd"));
    bool fields_ok = uv_field && color_field;
    for (int corner = 0; fields_ok && corner < uv_field->size; ++corner) {
      const auto a = uv_field->data<ccl::float2>()[corner];
      const auto b = color_field->data<ccl::packed_float3>()[corner];
      fields_ok &=
          std::abs(a.x - .2f) + std::abs(a.y - .7f) + std::abs(b.x - .3f) + std::abs(b.y - .4f) + std::abs(b.z - .5f) <
          2e-6;
    }
    require(fields_ok, "Retained UV/color fields differ from independent constant values");
    clear(mesh);
    changed.attributes[0].components = 3;
    changed.attributes[0].motion_values.clear();
    changed.attributes[0].values.assign(4, {.1f, .2f, .3f});
    require(refresh_mesh_shading(mesh, changed, false, {}, {}, {}, true),
            "Field type/motion changes rejected retained subdivision");
    refreshed = mesh.attributes.find(ccl::ustring(engine_attribute_name("sample")));
    require(refreshed && refreshed->type == ccl::TypeVector && refreshed->num_motion_steps() == 1 &&
                !mesh.attributes.find(ccl::ATTR_STD_GENERATED) &&
                !mesh.attributes.find(ccl::ATTR_STD_RANDOM_PER_ISLAND) &&
                !mesh.attributes.find(ccl::ATTR_STD_POINTINESS),
            "Field type/motion changes kept obsolete buffers or intrinsic demand");
    require(snapshot(mesh) == geometry && tessellations == 1, "Field retirement changed geometry or normal motion");
    clear(mesh);
    mesh.validated_displacement_reuse = true;
    require(mesh.can_reuse_displacement(), "Validated unchanged geometry did not permit displacement reuse");
    mesh.tag_shader_modified();
    require(mesh.can_reuse_displacement(), "A shader-ID-only update invalidated validated displacement");
    clear(mesh);
    mesh.subdivision_shading_samples->ready = false;
    require(!mesh.interpolate_subdivision_shading(), "Incomplete dicing exposed a reusable sampling map");
    mesh.subdivision_shading_samples->ready = true;
    mesh.tag_position_modified();
    require(!mesh.can_reuse_displacement() && !mesh.interpolate_subdivision_shading(),
            "Changed geometry incorrectly reused displacement or interpolation");
    clear(mesh);
    mesh.subd_attributes.find(ccl::ATTR_STD_POSITION)->data_for_write<ccl::packed_float3>();
    mesh.subd_attributes.find(ccl::ATTR_STD_POSITION)->modified = true;
    require(!mesh.interpolate_subdivision_shading(),
            "A changed coarse position buffer was consumed by the attribute-only path");
    clear(mesh);
    // A dense variant may complete dicing without retaining interpolation samples.
    require(refresh_mesh_shading(mesh, changed, true, {}, islands, curvature, true),
            "Could not prepare retained derived fields before removing the optional map");
    clear(mesh);
    {
      auto samples = std::move(mesh.subdivision_shading_samples);
      auto fewer_fields = changed;
      fewer_fields.attributes.erase(fewer_fields.attributes.begin());
      const auto generated = mesh.attributes.find(ccl::ATTR_STD_GENERATED)->data();
      require(refresh_mesh_shading(mesh, fewer_fields, false, {}, {}, {}, true),
              "Removal of shading demand required an unavailable interpolation map");
      require(mesh.attributes.find(ccl::ATTR_STD_GENERATED)->data() == generated &&
                  !mesh.attributes.find(ccl::ustring(engine_attribute_name("sample"))),
              "Retaining derived coordinates also retained a deleted named field");
      clear(mesh);
      require(refresh_mesh_shading(mesh, fewer_fields, true, {}, islands, curvature, true),
              "Reusing previously evaluated derived fields required fresh interpolation");
      require(mesh.attributes.find(ccl::ATTR_STD_GENERATED)->data() == generated && snapshot(mesh) == geometry,
              "Shading-only work replaced retained coordinate data or changed geometry");
      clear(mesh);
      require(!refresh_mesh_shading(mesh, changed, true, {}, islands, curvature, true),
              "A newly required shading field was accepted without interpolation samples");
      mesh.subdivision_shading_samples = std::move(samples);
    }
    clear(mesh);
    std::vector<int> remap(mesh.num_verts());
    std::iota(remap.begin(), remap.end(), 0);
    remap.push_back(mesh.get_triangles()[0]);
    mesh.remap_subdivision_shading_vertices(remap.data(), remap.size());
    require(mesh.subdivision_shading_samples && mesh.subdivision_shading_samples->vertices.back().patch ==
                                                    mesh.subdivision_shading_samples->vertices[remap.back()].patch,
            "Seam duplication lost owned vertex sampling provenance");
    mesh.clear();
    require(budget->used == 0 && !mesh.subdivision_shading_samples && !mesh.validated_displacement_reuse,
            "Clearing a mesh leaked sampling maps/budget or displacement validity");
  }
  auto tiny = std::make_shared<ccl::SubdivisionShadingBudget>(128);
  require(tiny->reserve(64) && !tiny->reserve(65) && tiny->used == 64,
          "Budget accepted a reservation over its session-wide limit");
  tiny->release(64);
  require(!ccl::SubdivisionShadingSamples::create(tiny, 1000, 1000) && tiny->used == 0,
          "Oversized optional storage consumed budget after declining reuse");
  require(!ccl::SubdivisionShadingSamples::create(tiny, std::numeric_limits<std::size_t>::max(), 1),
          "Overflowing sampling-map sizes were accepted");
  auto parallel_budget = std::make_shared<ccl::SubdivisionShadingBudget>(8192);
  ccl::parallel_for(ccl::blocked_range<int>(0, 128, 1), [&](const auto &range) {
    for (int i = range.begin(); i < range.end(); ++i) {
      auto p = ccl::SubdivisionShadingSamples::create(parallel_budget, 16, 32);
      if (parallel_budget->used > parallel_budget->limit)
        throw std::runtime_error("Concurrent sampling-map allocation exceeded its budget");
    }
  });
  require(parallel_budget->used == 0, "Concurrent map retirement leaked its shared budget");
  // Publish the completed contract result before native runtime shutdown.
  // Buffered stdout can otherwise be lost while linked runtimes retire.
  std::cout << "{\"result\":\"passed\",\"subdivision_shading_checks\":" << checks
            << ",\"subdivision_count_overflow_checks\":" << count_overflow_checks << ",\"types\":2}" << std::endl;
}
} // namespace cycles_houdini::harness
