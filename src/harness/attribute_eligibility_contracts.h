// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "instance_motion_contracts.h"
#include "util/tbb.h"
#include <cstdlib>
#include <iomanip>
#include <limits>
#include <sstream>

namespace cycles_houdini::harness {
class AttributeReferenceSwitch {
public:
  explicit AttributeReferenceSwitch(const char *name) : name_(name) {
    if (const char *value = std::getenv(name_))
      saved_ = value;
  }
  ~AttributeReferenceSwitch() { set(saved_ ? saved_->c_str() : nullptr); }

  void set(const char *value) const {
#ifdef _WIN32
    _putenv_s(name_, value ? value : "");
#else
    if (value)
      setenv(name_, value, 1);
    else
      unsetenv(name_);
#endif
  }

private:
  const char *name_;
  std::optional<std::string> saved_;
};

inline int sampled_attribute_contracts(SessionConfig config) {
  AttributeReferenceSwitch sampled_reference("CYCLES_HOUDINI_SAMPLED_ATTRIBUTES_REFERENCE");

  config.width = 80;
  config.height = 60;
  config.samples = 16;
  config.passes = {"combined", "depth", "object_id"};
  auto scene = instance_field_scene(0, {{"Cd", 3, {.2f, .4f, .8f}}, {"Cd", 3, {.2f, .6f, .4f}}}, 0, "sampled");
  scene.rendering = RenderSettings{};
  scene.rendering->samples = config.samples;
  scene.rendering->adaptive_sampling = false;
  auto &field_only = scene.objects[0];
  auto &attached = scene.objects[1];
  field_only.motion = linear_transform_motion(field_only.transform);
  field_only.nominal_transform = field_only.transform;
  field_only.nominal_transform->at(7) += .15f;
  auto pose_only = attached;
  pose_only.key += "/pose-only";
  pose_only.transform[7] = .85f;
  field_only.properties.pass_index = 4;
  attached.properties.pass_index = 7;
  pose_only.properties.pass_index = 9;
  RenderSession reference(config), candidate(config);
  int compared = 0;
  const auto same_images = [&](const FrameEvent &a, const FrameEvent &b, const std::string &label) {
    motion_require(a.planes.size() == b.planes.size(), label + ": image plane count differs");
    for (std::size_t p = 0; p < a.planes.size(); ++p) {
      const auto &x = a.planes[p], &y = b.planes[p];
      motion_require(x.pass == y.pass && x.channels == y.channels && x.pixels.size() == y.pixels.size(),
                     label + ": image plane layout differs");
      for (std::size_t n = 0; n < x.pixels.size(); ++n)
        if (!std::isfinite(x.pixels[n]) ||
            std::bit_cast<std::uint32_t>(x.pixels[n]) != std::bit_cast<std::uint32_t>(y.pixels[n]))
          throw std::runtime_error(label + ": different or nonfinite pixel in " + x.pass);
    }
  };
  const auto apply = [&](const SceneUpdate &u, const std::string &label, std::size_t direct = 0,
                         const char *fallback = nullptr) {
    std::cerr << "SAMPLED_ATTRIBUTE_CASE " << label << '\n';
    sampled_reference.set("1");
    const auto a = motion_wait(reference, reference.apply(u));
    sampled_reference.set("0");
    const auto b = motion_wait(candidate, candidate.apply(u));
    same_images(*a, *b, label);
    const auto before = reference.status(), after = candidate.status();
    motion_require(before.direct_attribute_updates == 0 &&
                       before.expanded_attribute_updates == u.object_attributes.size() &&
                       after.direct_attribute_updates == direct &&
                       after.expanded_attribute_updates == u.object_attributes.size() - direct &&
                       (!fallback || after.attribute_fallback_reason == fallback),
                   label + ": incorrect direct/fallback classification: " + after.attribute_fallback_reason);
    motion_require(before.generation == after.generation && before.object_updates == after.object_updates &&
                       before.object_resources == after.object_resources &&
                       before.geometry_resources == after.geometry_resources &&
                       before.geometry_conversions == after.geometry_conversions,
                   label + ": accepted resource/update counts differ");
    ++compared;
    return b;
  };
  auto full = scene;
  full.objects.push_back(pose_only);
  const auto initial = apply(full, "blurred baseline");
  auto moved = attached.transform;
  moved[3] -= .25f;
  auto nominal = moved;
  nominal[7] += .2f;
  SceneUpdate fields;
  fields.motion_blur = true;
  fields.object_attributes = {
      {attached.key,
       {{"Cd", 3, {.85f, .2f, .1f}}},
       ObjectPoseData{attached.key, moved, linear_transform_motion(moved), nominal}},
      {field_only.key, {{"Cd", 3, {.1f, .8f, .3f}}}},
  };
  fields.object_poses = {{pose_only.key, pose_only.transform, linear_transform_motion(pose_only.transform)}};
  const auto changed = apply(fields, "mixed retained motion, attached shutter pose and disjoint pose", 2);
  motion_require(initial->planes.front().pixels != changed->planes.front().pixels,
                 "blurred edit did not affect pixels");
  full.objects[0].attributes = fields.object_attributes[1].attributes;
  full.objects[1].attributes = fields.object_attributes[0].attributes;
  full.objects[1].transform = moved;
  full.objects[1].nominal_transform = nominal;
  full.objects[1].motion = fields.object_attributes[0].pose->motion;
  full.objects[2].motion = fields.object_poses[0].motion;
  RenderSession fresh(config);
  same_images(*changed, *motion_wait(fresh, fresh.apply(full)), "fresh full scene with object motion");
  fresh.close();

  SceneUpdate clear;
  clear.object_attributes = {{attached.key, full.objects[1].attributes, ObjectPoseData{attached.key, moved}}};
  apply(clear, "complete attached pose removes motion and nominal transform", 1);
  const auto accepted = candidate.status();
  const auto image = candidate.frame();
  auto invalid = fields;
  invalid.object_attributes[0].pose->motion[1][3] += 1.f;
  bool rejected = false;
  try {
    candidate.apply(invalid);
  } catch (const SceneUpdateRejected &e) {
    rejected = std::string_view(e.what()).find("centre differs") != std::string_view::npos;
  }
  const auto status = candidate.status();
  motion_require(rejected && image == candidate.frame() && status.generation == accepted.generation &&
                     status.object_updates == accepted.object_updates &&
                     status.direct_attribute_updates == accepted.direct_attribute_updates &&
                     status.expanded_attribute_updates == accepted.expanded_attribute_updates &&
                     status.attribute_fallback_count == accepted.attribute_fallback_count,
                 "invalid shutter pose changed the accepted state");
  apply(fields, "restore motion after rejection", 2);

  auto temporal = fields;
  temporal.object_attributes[1].attributes[0].motion_values = {{.1f, .1f, .3f}, {.1f, .8f, .3f}, {.7f, .8f, .3f}};
  apply(temporal, "sampled field remains on full validation", 1, "attribute_schema_or_motion");
  apply(fields, "restore an unsampled field from a sampled field", 1, "attribute_schema_or_motion");
  apply(fields, "both fields are eligible again", 2);

  auto vectors = fields;
  vectors.motion_blur = false;
  vectors.passes = config.passes;
  vectors.passes->push_back("motion");
  apply(vectors, "motion pass and blur transition require expansion", 0, "passes");
  auto steady_vectors = fields;
  steady_vectors.motion_blur = false;
  const auto motion = apply(steady_vectors, "steady motion-vector render requires expansion", 0, "sampled_transforms");
  motion_require(std::any_of(motion->planes.begin(), motion->planes.end(),
                             [](const auto &plane) {
                               return plane.pass == "motion" &&
                                      std::any_of(plane.pixels.begin(), plane.pixels.end(),
                                                  [](float v) { return std::abs(v) > .001f; });
                             }),
                 "motion-vector test did not produce visible vectors");
  vectors.motion_blur = true;
  vectors.passes = config.passes;
  apply(vectors, "return to blur requires expansion", 0, "sampled_transforms");
  apply(fields, "steady blur after restoring original passes", 2);
  return compared;
}

inline void attribute_eligibility_contracts(const std::string &device) {
  constexpr std::size_t count = 8256; // 64 chunks of 129: duplicate fixture crosses a chunk boundary.
  AttributeReferenceSwitch eligibility_reference("CYCLES_HOUDINI_ATTRIBUTE_ELIGIBILITY_REFERENCE");
  AttributeReferenceSwitch sampled_reference("CYCLES_HOUDINI_SAMPLED_ATTRIBUTES_REFERENCE");
  sampled_reference.set("0");
  int checks = 0, rejected = 0, rendered = 0;
  const auto require = [&](bool value, const std::string &message) {
    ++checks;
    if (!value)
      throw std::runtime_error("Attribute eligibility contract: " + message);
  };
  require(device == "CPU", "This exact-pixel fixture is CPU-only; GPU Threads uses independent variation review");
  const auto key = [](std::size_t n) {
    std::ostringstream out;
    out << "eligibility/object/" << std::setw(6) << std::setfill('0') << n;
    return out.str();
  };
  auto scene = instance_field_scene(0, {{"Cd", 3, {.2f, .4f, .8f}}}, 0, "eligibility", false, false);
  const auto prototype = scene.objects.front();
  scene.objects.clear();
  for (std::size_t n = 0; n < count; ++n) {
    auto object = prototype;
    object.key = key(n);
    object.transform[3] = n < 2 ? (n ? .8f : -.8f) : 10.f + float(n) * .01f;
    object.random_id = std::uint32_t(n);
    scene.objects.push_back(std::move(object));
  }
  SessionConfig config;
  config.device_id = device;
  config.width = 32;
  config.height = 24;
  config.samples = 1;
  config.threads = 0;
  config.interactive = false;
  config.persistent_updates = true;
  config.collect_diagnostics = true;
  RenderSession reference(config), candidate(config);
  const auto wait = [&](RenderSession &session, std::uint64_t generation) {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(45);
    for (;;) {
      const auto status = session.status();
      require(status.state != SessionState::Failed, status.error);
      const auto frame = session.frame();
      if (frame && frame->generation == generation && frame->complete)
        return frame;
      require(std::chrono::steady_clock::now() < deadline, "render timeout");
      std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
  };
  const auto compare = [&](const SceneUpdate &update, const std::string &label,
                           std::optional<std::uint64_t> direct = {}, const char *fallback = nullptr) {
    std::cerr << "ELIGIBILITY_CASE " << label << '\n';
    eligibility_reference.set("1");
    const auto a = wait(reference, reference.apply(update));
    const auto sa = reference.status();
    eligibility_reference.set("0");
    const auto b = wait(candidate, candidate.apply(update));
    const auto sb = candidate.status();
    require(a->planes.size() == b->planes.size(), label + ": plane inventory");
    double maximum = 0;
    for (std::size_t p = 0; p < a->planes.size(); ++p) {
      const auto &x = a->planes[p], &y = b->planes[p];
      require(x.pass == y.pass && x.channels == y.channels && x.pixels.size() == y.pixels.size(),
              label + ": incompatible pixels");
      for (std::size_t i = 0; i < x.pixels.size(); ++i) {
        require(std::isfinite(x.pixels[i]) && std::isfinite(y.pixels[i]), label + ": nonfinite pixels");
        maximum = std::max(maximum, std::abs(double(x.pixels[i]) - y.pixels[i]));
      }
    }
    require(maximum <= (device == "CPU" ? 0.0 : 1e-6), label + ": reference/candidate pixels differ");
    require(sa.generation == sb.generation && sa.object_updates == sb.object_updates &&
                sa.object_resources == sb.object_resources && sa.geometry_resources == sb.geometry_resources &&
                sa.direct_attribute_updates == sb.direct_attribute_updates &&
                sa.expanded_attribute_updates == sb.expanded_attribute_updates &&
                sa.attribute_fallback_count == sb.attribute_fallback_count &&
                sa.attribute_fallback_reason == sb.attribute_fallback_reason,
            label + ": status/path differs");
    if (direct)
      require(sa.direct_attribute_updates == *direct, label + ": wrong direct command count");
    if (fallback)
      require(sa.attribute_fallback_reason == fallback, label + ": wrong earliest fallback");
    ++rendered;
    return maximum;
  };
  compare(scene, "initial", 0);
  const auto initial = candidate.frame();
  const auto &initial_plane = initial->planes.front();
  float minimum_rgb = std::numeric_limits<float>::infinity(), maximum_rgb = 0;
  for (std::size_t i = 0; i < initial_plane.pixels.size(); ++i)
    if (i % std::size_t(initial_plane.channels) < 3) {
      minimum_rgb = std::min(minimum_rgb, initial_plane.pixels[i]);
      maximum_rgb = std::max(maximum_rgb, initial_plane.pixels[i]);
    }
  require(maximum_rgb > .02f && maximum_rgb - minimum_rgb > .02f, "initial image is blank or flat");
  const auto default_parallelism = tbb::global_control::active_value(tbb::global_control::max_allowed_parallelism);
  std::cerr << "ELIGIBILITY_TBB default=" << default_parallelism << '\n';
  SceneUpdate values;
  for (std::size_t n = 0; n < count; ++n) {
    auto transform = scene.objects[n].transform;
    transform[7] += .05f;
    values.object_attributes.push_back({key(n), {{"Cd", 3, {.8f, .3f, .1f}}}, ObjectPoseData{key(n), transform}});
  }
  compare(values, "all-eligible-sorted", count);
  const auto changed = candidate.frame();
  bool visible_change = false;
  for (std::size_t i = 0; i < initial_plane.pixels.size(); ++i)
    if (i % std::size_t(initial_plane.channels) < 3)
      visible_change |= std::abs(initial_plane.pixels[i] - changed->planes.front().pixels[i]) > 1e-5f;
  require(visible_change, "Cd/pose update did not change visible pixels");
  {
    tbb::global_control one(tbb::global_control::max_allowed_parallelism, 1);
    require(tbb::global_control::active_value(tbb::global_control::max_allowed_parallelism) == 1,
            "TBB single-thread control not active");
    std::cerr << "ELIGIBILITY_TBB controlled=1\n";
    compare(values, "all-eligible-tbb-one", count);
  }
  require(tbb::global_control::active_value(tbb::global_control::max_allowed_parallelism) == default_parallelism,
          "TBB default control not restored");
  compare(values, "all-eligible-tbb-default", count);
  std::reverse(values.object_attributes.begin(), values.object_attributes.end());
  compare(values, "all-eligible-reversed", count);
  std::reverse(values.object_attributes.begin(), values.object_attributes.end());

  const auto invalid = [&](const SceneUpdate &update, const std::string &label, const std::string &expected) {
    std::cerr << "ELIGIBILITY_CASE " << label << '\n';
    const auto refuse = [&](RenderSession &session, const char *mode) {
      eligibility_reference.set(mode);
      const auto before = session.status();
      const auto image = session.frame();
      std::string message;
      try {
        session.apply(update);
      } catch (const SceneUpdateRejected &error) {
        message = error.what();
      }
      require(!message.empty(), label + ": did not reject before mutation");
      const auto after = session.status();
      require(before.generation == after.generation && before.object_updates == after.object_updates &&
                  before.direct_attribute_updates == after.direct_attribute_updates &&
                  before.geometry_resources == after.geometry_resources &&
                  before.owned_geometry_bytes == after.owned_geometry_bytes && image == session.frame(),
              label + ": rejection changed accepted state");
      return message;
    };
    const auto a = refuse(reference, "1"), b = refuse(candidate, "0");
    require(a == b && a.find(expected) != std::string::npos, label + ": first error differs: " + a + " / " + b);
    ++rejected;
  };
  auto bad = values;
  bad.object_attributes[129] = bad.object_attributes[128];
  invalid(bad, "duplicate-at-chunk-boundary", "Invalid or duplicate object attribute key");
  bad = values;
  bad.object_attributes[50].pose->key += "/mismatch";
  bad.object_attributes[7000].attributes[0].value.x = std::numeric_limits<float>::quiet_NaN();
  std::reverse(bad.object_attributes.begin(), bad.object_attributes.end());
  invalid(bad, "sorted-error-before-original-index", "Attached object pose key differs");
  bad = values;
  bad.object_attributes[100].pose->transform[0] = std::numeric_limits<float>::quiet_NaN();
  bad.object_attributes[100].attributes.push_back({"latent", 1, {.1f, 0, 0}});
  invalid(bad, "invalid-transform-before-eligibility", "finite");
  bad = values;
  bad.object_attributes[100].attributes[0].value.x = std::numeric_limits<float>::infinity();
  invalid(bad, "nonfinite-field", "Invalid owned instance attribute value");
  bad = values;
  bad.object_attributes[50].key = "eligibility/object/999999";
  bad.object_attributes[50].pose.reset();
  invalid(bad, "missing-object", "Object attributes reference a missing object");
  bad = values;
  bad.object_poses = {*bad.object_attributes[130].pose};
  invalid(bad, "ordered-pose-conflict", "Object attributes conflict");
  bad = values;
  bad.object_poses = {*bad.object_attributes[7000].pose, *bad.object_attributes[130].pose};
  invalid(bad, "unordered-pose-conflict", "Object attributes conflict");
  bad = values;
  bad.deleted_objects = {bad.object_attributes[130].key};
  invalid(bad, "deletion-conflict", "Object attributes conflict");
  bad = values;
  bad.objects = {scene.objects[130]};
  invalid(bad, "upsert-conflict", "Object attributes conflict");
  bad = values;
  bad.object_attributes[10].attributes.push_back({"latent", 1, {.1f, 0, 0}});
  bad.object_attributes[7000].pose->key += "/mismatch";
  invalid(bad, "fallback-before-later-error", "Attached object pose key differs");
  bad = values;
  bad.object_attributes[10].pose->key += "/mismatch";
  bad.object_attributes[7000].attributes.push_back({"latent", 1, {.1f, 0, 0}});
  invalid(bad, "error-before-later-fallback", "Attached object pose key differs");
  compare(values, "valid-after-rejections", count);

  auto mixed = values;
  mixed.object_attributes[128].attributes.push_back({"latent", 1, {.1f, 0, 0}});
  mixed.object_attributes[8000].attributes[0].motion_complete = false;
  std::reverse(mixed.object_attributes.begin(), mixed.object_attributes.end());
  compare(mixed, "first-fallback-by-sorted-position", count - 2, "attribute_count");
  compare(values, "restore-schema", count - 2, "attribute_count");
  auto full = scene;
  for (std::size_t n = 0; n < count; ++n) {
    full.objects[n].attributes = values.object_attributes[n].attributes;
    full.objects[n].transform = values.object_attributes[n].pose->transform;
  }
  RenderSession fresh(config);
  const auto independent = wait(fresh, fresh.apply(full));
  const auto retained = candidate.frame();
  require(independent->planes.size() == retained->planes.size(), "fresh full scene plane inventory");
  for (std::size_t p = 0; p < independent->planes.size(); ++p)
    require(independent->planes[p].pixels == retained->planes[p].pixels,
            "compact update differs from independent fresh full scene");
  fresh.close();
  auto blur = values;
  blur.motion_blur = true;
  compare(blur, "blur-transition-stays-fallback", 0, "motion_blur");
  const auto before_steady_blur = candidate.frame();
  for (auto &attribute : blur.object_attributes) {
    auto &pose = *attribute.pose;
    pose.motion = linear_transform_motion(pose.transform);
    attribute.attributes.front().value = {.15f, .7f, .35f};
  }
  compare(blur, "steady-blur-eligible", count);
  require(before_steady_blur->planes.front().pixels != candidate.frame()->planes.front().pixels,
          "steady-blur field and shutter-pose changes did not change visible pixels");
  reference.close();
  candidate.close();
  require(!reference.status().owned_geometry_bytes && !candidate.status().owned_geometry_bytes,
          "closed sessions retained geometry");
  const auto sampled_pairs = sampled_attribute_contracts(config);
  std::cout << "{\"attribute_eligibility_contracts\":true,\"checks\":" << checks << ",\"records\":" << count
            << ",\"tbb_default_parallelism\":" << default_parallelism << ",\"rejected\":" << rejected
            << ",\"rendered_pairs\":" << rendered << ",\"sampled_pairs\":" << sampled_pairs << "}\n";
}
} // namespace cycles_houdini::harness
