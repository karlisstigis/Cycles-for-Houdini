// SPDX-License-Identifier: Apache-2.0
#include "session_internal.h"

namespace cycles_houdini {
void RenderSession::Impl::apply_linking(ccl::Object *object, const std::string &id) {
  const auto receiver = linking_plan.receivers.set(id), blocker = linking_plan.blockers.set(id);
  const auto light_mask = linking_plan.receivers.membership(id), shadow_mask = linking_plan.blockers.membership(id);
  if (object->get_receiver_light_set() == receiver && object->get_blocker_shadow_set() == blocker &&
      object->get_light_set_membership() == light_mask && object->get_shadow_set_membership() == shadow_mask)
    return;
  object->set_receiver_light_set(receiver);
  object->set_blocker_shadow_set(blocker);
  object->set_light_set_membership(light_mask);
  object->set_shadow_set_membership(shadow_mask);
  object->tag_update(session->scene.get());
  session->scene->light_manager->tag_update(session->scene.get(), ccl::LightManager::UPDATE_ALL);
}

ccl::unique_ptr<ccl::ShaderGraph> RenderSession::Impl::light_cone_graph(const LightCone &cone,
                                                                        ccl::unique_ptr<ccl::ShaderGraph> graph) {
  // Geometry Incoming at an analytic emitter points from the light to
  // the receiver. Convert it to light-local space before evaluating the
  // authored cone. This also supports cones on area and spherical lights.
  if (!graph) {
    graph = ccl::make_unique<ccl::ShaderGraph>();
    auto *emission = graph->create_node<ccl::EmissionNode>();
    emission->set_color(ccl::make_float3(1));
    emission->set_strength(1);
    graph->connect(emission->output("Emission"), graph->output()->input("Surface"));
  }
  auto *geometry = graph->create_node<ccl::GeometryNode>();
  auto *local = graph->create_node<ccl::VectorTransformNode>();
  graph->connect(geometry->output("Incoming"), local->input("Vector"));
  auto *normalize = graph->create_node<ccl::VectorMathNode>();
  normalize->set_math_type(ccl::NODE_VECTOR_MATH_NORMALIZE);
  graph->connect(local->output("Vector"), normalize->input("Vector1"));
  auto *cosine = graph->create_node<ccl::VectorMathNode>();
  cosine->set_math_type(ccl::NODE_VECTOR_MATH_DOT_PRODUCT);
  cosine->set_vector2(ccl::make_float3(0, 0, cone.reverse_axis ? 1 : -1));
  graph->connect(normalize->output("Vector"), cosine->input("Vector1"));
  ccl::ShaderOutput *coverage{};
  if (std::cos(cone.inner_half_angle) < -.999f) {
    auto *all = graph->create_node<ccl::ValueNode>();
    all->set_value(1);
    coverage = all->output("Value");
  } else if (cone.outer_half_angle == cone.inner_half_angle) {
    auto *edge = graph->create_node<ccl::MathNode>();
    edge->set_math_type(ccl::NODE_MATH_GREATER_THAN);
    edge->set_value2(std::cos(cone.inner_half_angle));
    graph->connect(cosine->output("Value"), edge->input("Value1"));
    coverage = edge->output("Value");
  } else {
    auto *ramp = graph->create_node<ccl::MapRangeNode>();
    ramp->set_range_type(ccl::NODE_MAP_RANGE_SMOOTHSTEP);
    ramp->set_clamp(true);
    ramp->set_from_min(std::cos(cone.outer_half_angle));
    ramp->set_from_max(std::cos(cone.inner_half_angle));
    ramp->set_to_min(0);
    ramp->set_to_max(1);
    graph->connect(cosine->output("Value"), ramp->input("Value"));
    coverage = ramp->output("Result");
    if (cone.rolloff != 1) {
      auto *power = graph->create_node<ccl::MathNode>();
      power->set_math_type(ccl::NODE_MATH_POWER);
      power->set_value2(cone.rolloff >= 1 ? cone.rolloff : 1 / cone.rolloff);
      if (cone.rolloff >= 1)
        graph->connect(coverage, power->input("Value1"));
      else {
        auto *inverse = graph->create_node<ccl::MathNode>();
        inverse->set_math_type(ccl::NODE_MATH_SUBTRACT);
        inverse->set_value1(1);
        graph->connect(coverage, inverse->input("Value2"));
        graph->connect(inverse->output("Value"), power->input("Value1"));
      }
      coverage = power->output("Value");
      if (cone.rolloff < 1) {
        auto *inverse = graph->create_node<ccl::MathNode>();
        inverse->set_math_type(ccl::NODE_MATH_SUBTRACT);
        inverse->set_value1(1);
        graph->connect(coverage, inverse->input("Value2"));
        coverage = inverse->output("Value");
      }
    }
  }
  auto *surface = graph->output()->input("Surface");
  auto *source = surface->link;
  graph->disconnect(surface);
  auto *mix = graph->create_node<ccl::MixClosureNode>();
  graph->connect(coverage, mix->input("Fac"));
  graph->connect(source, mix->input("Closure2"));
  graph->connect(mix->output("Closure"), surface);
  return graph;
}

void RenderSession::Impl::update_light(const LightData &l, ccl::unique_ptr<ccl::ShaderGraph> prepared) {
  auto &r = lights[l.key];
  if (r.definition && *r.definition == l && !prepared)
    return;
  if (r.node && r.kind != l.kind) {
    session->scene->delete_nodes(ccl::set<ccl::Object *>{r.object});
    session->scene->delete_nodes(ccl::set<ccl::Geometry *>{r.node});
    r.node = nullptr;
    r.object = nullptr;
  }
  if (!r.node) {
    if (l.kind == LightKind::Area)
      r.node = session->scene->create_node<ccl::AreaLight>();
    else if (l.kind == LightKind::Sun)
      r.node = session->scene->create_node<ccl::SunLight>();
    else if (l.kind == LightKind::Spot)
      r.node = session->scene->create_node<ccl::SpotLight>();
    else
      r.node = session->scene->create_node<ccl::PointLight>();
    r.kind = l.kind;
    r.node->name = ccl::ustring(l.key);
    r.object = session->scene->create_node<ccl::Object>();
    r.object->name = ccl::ustring(l.key);
    r.object->set_geometry(r.node);
  }
  if (l.cone || l.graph) {
    if (!r.cone_shader) {
      if (retired_shaders.empty())
        r.cone_shader = session->scene->create_node<ccl::Shader>();
      else {
        r.cone_shader = retired_shaders.back();
        retired_shaders.pop_back();
        ++shader_reuses;
      }
      r.cone_shader->name = ccl::ustring(l.key + "/emission");
    }
    if (prepared || !r.definition || r.definition->cone != l.cone || r.definition->graph != l.graph) {
      if (l.cone)
        prepared = light_cone_graph(*l.cone, std::move(prepared));
      if (!prepared)
        throw std::logic_error("A light emission graph was not prepared before mutation");
      r.cone_shader->set_graph(std::move(prepared));
      r.cone_shader->tag_update(session->scene.get());
    }
  }
  ccl::array<ccl::Node *> shaders;
  shaders.reserve(1);
  auto *shader = (l.cone || l.graph) ? r.cone_shader : session->scene->default_light;
  shader->tag_used(session->scene.get());
  shaders.push_back_reserved(shader);
  r.node->set_used_shaders(shaders);
  if (!l.cone && !l.graph && r.cone_shader) {
    retire_shader(r.cone_shader);
    r.cone_shader = nullptr;
  }
  r.node->set_strength(vec(l.strength));
  r.node->set_normalize(l.normalize);
  r.node->set_cast_shadow(l.cast_shadow);
  r.node->set_use_mis(l.use_mis);
  r.node->set_max_bounces(l.max_bounces);
  r.node->set_use_caustics(l.use_caustics);
  if (l.kind == LightKind::Point || l.kind == LightKind::Spot) {
    auto *p = static_cast<ccl::PointLight *>(r.node);
    p->set_radius(l.radius);
    p->set_is_sphere(l.is_sphere);
    if (l.kind == LightKind::Spot) {
      auto *spot = static_cast<ccl::SpotLight *>(p);
      spot->set_angle(l.angle);
      spot->set_smooth(l.spot_smooth);
    }
  } else if (l.kind == LightKind::Area) {
    auto *a = static_cast<ccl::AreaLight *>(r.node);
    a->set_sizeu(l.size_u);
    a->set_sizev(l.size_v);
    a->set_ellipse(l.ellipse);
    a->set_spread(l.spread);
    a->set_is_portal(l.is_portal);
  } else
    static_cast<ccl::SunLight *>(r.node)->set_angle(l.angle);
  r.node->tag_update(session->scene.get());
  r.object->set_tfm(transform(l.transform));
  r.object->set_visibility(l.visibility);
  r.object->set_is_shadow_catcher(l.shadow_catcher);
  r.object->set_lightgroup(ccl::ustring(l.lightgroup));
  // apply_linking tags its own object update when a set changes.
  apply_linking(r.object, l.linking_id.empty() ? l.key : l.linking_id);
  const auto name = ccl::ustring(l.cryptomatte_object.empty() ? l.key : l.cryptomatte_object);
  if (r.object->name != name) {
    r.object->name = name;
    r.object->tag_asset_name_modified();
  }
  r.object->set_asset_name(ccl::ustring(l.cryptomatte_asset.empty() ? name.string() : l.cryptomatte_asset));
  // Tag the light's Object only when one of its sockets changed, as Blender's sync
  // does. Object::tag_update always notifies the object and geometry managers, so an
  // unconditional tag made a pure strength/colour/shape edit rebuild the scene BVH,
  // bounds, object table and attributes. The light itself (strength, size, shader)
  // is tagged above through Light::tag_update, which updates the light manager.
  if (r.object->is_modified())
    r.object->tag_update(session->scene.get());
  r.definition = l;
  ++light_updates;
}

void RenderSession::Impl::sync_world(const SceneUpdate &u, PreparedUpdate &prepared, bool linking_plan_changed) {
  auto &i = *this;
  auto *scene = i.session->scene.get();
  auto &effective_world = prepared.effective_world;
  auto &world_changed = prepared.world_changed;
  auto &world_graph_changed = prepared.world_graph_changed;
  auto &world_requirements = prepared.world_requirements;
  auto &prepared_world = prepared.prepared_world;
  const bool background_changed = u.background && i.background_definition != u.background;
  if (prepared_world ||
      (!effective_world.graph && (background_changed || world_graph_changed || i.color_update_pending))) {
    if (!prepared_world) {
      prepared_world = ccl::make_unique<ccl::ShaderGraph>();
      auto *n = prepared_world->create_node<ccl::BackgroundNode>();
      n->set_color(vec(u.background.value_or(i.background_definition.value_or(Float3{}))));
      n->set_strength(1);
      prepared_world->connect(n->output("Background"), prepared_world->output()->input("Surface"));
      world_requirements = {};
    }
    scene->default_background->set_graph(std::move(prepared_world));
    scene->default_background->tag_update(scene);
    scene->background->tag_update(scene);
    i.world_requirements = std::move(world_requirements);
    ++i.background_builds;
  }
  if (u.background)
    i.background_definition = *u.background;
  if (linking_plan_changed) {
    for (const auto &[key, record] : i.objects)
      i.apply_linking(record.node, record.properties.linking_id.empty() ? key : record.properties.linking_id);
    for (const auto &[key, record] : i.lights)
      i.apply_linking(record.object, record.definition && !record.definition->linking_id.empty()
                                         ? record.definition->linking_id
                                         : key);
    i.apply_linking(i.background_object, "world");
  }
  if (world_changed) {
    auto *light = static_cast<ccl::BackgroundLight *>(i.background_object->get_geometry());
    light->set_use_mis(u.world->use_mis);
    light->set_cast_shadow(u.world->cast_shadow);
    light->set_use_caustics(u.world->use_caustics);
    light->set_max_bounces(u.world->max_bounces);
    light->set_map_resolution(u.world->map_resolution);
    light->tag_update(scene);
    scene->background->set_visibility(u.world->visibility);
    i.background_object->set_visibility(u.world->visibility & ~ccl::PATH_RAY_VISIBILITY_CAMERA);
    i.background_object->tag_update(scene);
    // Per-world volume sampling, as Blender's World > Volume panel.
    const auto sampling = static_cast<ccl::VolumeSampling>(u.world->volume_sampling);
    if (scene->default_background->get_volume_sampling_method() != sampling) {
      scene->default_background->set_volume_sampling_method(sampling);
      scene->default_background->tag_update(scene);
    }
    i.world_definition = *u.world;
  }
  if (u.transparent_background)
    scene->background->set_transparent(*u.transparent_background);
  if (u.background_shadow_catcher) {
    i.background_object->set_is_shadow_catcher(*u.background_shadow_catcher);
    i.background_object->tag_update(scene);
    scene->background->set_is_shadow_catcher(*u.background_shadow_catcher);
    scene->background->tag_update(scene);
    i.background_shadow_catcher_definition = *u.background_shadow_catcher;
  }
  if (u.background_lightgroup) {
    const auto group = ccl::ustring(*u.background_lightgroup);
    // Miss/emission paths read KernelBackground; sampled world lighting
    // reads the BackgroundLight's Object. Both identities must agree.
    scene->background->set_lightgroup(group);
    scene->background->tag_update(scene);
    i.background_object->set_lightgroup(group);
    i.background_object->tag_update(scene);
    i.background_lightgroup_definition = *u.background_lightgroup;
  }
  if (u.transparent_glass)
    scene->background->set_transparent_glass(*u.transparent_glass);
  if (u.transparent_roughness_threshold)
    scene->background->set_transparent_roughness_threshold(*u.transparent_roughness_threshold);
  if (u.transparent_background)
    i.transparent_background_definition = u.transparent_background;
  if (u.transparent_glass)
    i.transparent_glass_definition = u.transparent_glass;
  if (u.transparent_roughness_threshold)
    i.transparent_roughness_definition = u.transparent_roughness_threshold;
  if (u.motion_blur)
    i.motion_blur_definition = u.motion_blur;
  if (u.transparent_background || u.transparent_glass || u.transparent_roughness_threshold)
    scene->background->tag_update(scene);
}

void RenderSession::Impl::sync_lights(PreparedUpdate &prepared) {
  auto &i = *this;
  auto *scene = i.session->scene.get();
  auto &effective_lights = prepared.effective_lights;
  auto &prepared_lights = prepared.prepared_lights;
  for (const auto &light : effective_lights) {
    const auto prepared = prepared_lights.find(light.key);
    i.update_light(light, prepared == prepared_lights.end() ? ccl::unique_ptr<ccl::ShaderGraph>{}
                                                            : std::move(prepared->second));
  }
}

void RenderSession::Impl::delete_lights(const SceneUpdate &u) {
  auto &i = *this;
  auto *scene = i.session->scene.get();
  for (const auto &k : u.deleted_lights) {
    if (auto it = i.lights.find(k); it != i.lights.end()) {
      scene->delete_nodes(ccl::set<ccl::Object *>{it->second.object});
      scene->delete_nodes(ccl::set<ccl::Geometry *>{it->second.node});
      i.retire_shader(it->second.cone_shader);
      i.lights.erase(it);
    }
    i.light_watch_paths.erase(k);
    i.light_watch_revisions.erase(k);
    i.light_watch_auto.erase(k);
  }
}

} // namespace cycles_houdini
