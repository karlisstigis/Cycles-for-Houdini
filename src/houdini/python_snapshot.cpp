// SPDX-License-Identifier: Apache-2.0
#include "python_binding_state.h"
#include <pybind11/stl.h>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <map>
#include <mutex>
#include <set>
#include <string_view>
#include "../core/color_internal.h"
#include "cycles_houdini/volume.h"
#include <OP/OP_Director.h>
#include <UT/UT_Thread.h>

namespace cycles_houdini::python_detail {
// Host freezing and pure owned rebasing are separate. Neither touches the
// adapter, its in-progress transaction, source diagnostics or texture policies.
struct NavigationUpdate {
  double time{};
  ch::NativeMotionSettings motion;
  std::uint64_t root_revision{};
  ch::SceneUpdate camera;
  std::map<std::string, ch::NativeNavigationState::Root> roots;
  struct LightPose {
    std::string path;
    ch::Matrix transform;
    ch::Matrix baseline_transform;
    bool operator==(const LightPose &) const = default;
  };
  std::map<std::string, LightPose> light_poses;
};

using NavigationPtr = std::shared_ptr<const NavigationUpdate>;

using FrameReceiver = std::shared_ptr<const ch::ColorConfiguration>;

NavigationPtr navigation_update(py::handle value);

py::object apply_snapshot(py::handle capsule_arg, py::handle desired_arg, py::handle ack_arg) {
  py::handle capsule, desired, ack = py::none();
  capsule = capsule_arg;
  desired = desired_arg;
  if (!is_omitted(ack_arg))
    ack = ack_arg;
  auto &h = handle(capsule);
  ApplyGuard guard{h};
  auto owned = snapshot(desired);
  auto previous = ack.is_none() ? SnapshotPtr{} : snapshot(ack);
  std::uint64_t generation;
  {
    ReleaseGIL release;
    // An in-process engine has no root-pose derivation (protocol 91 is the
    // worker's): give it every instance pose.
    owned = materialize_root_poses(owned);
    previous = materialize_root_poses(previous);
    if (ch::current_color_configuration()->policy().render_policy_id != owned->color->policy().render_policy_id)
      throw ch::SceneUpdateRejected("Captured working color policy was superseded; recapture scene");
    const auto update = ch::snapshot_difference(previous ? previous->state.get() : nullptr, *owned->state);
    const auto before = h.policy_slot->replace(owned->textures);
    try {
      generation = h.session->apply(update, owned->reuse_if_unchanged);
    } catch (...) {
      h.policy_slot->replace(before);
      throw;
    }
  }
  return py::int_(generation);
}

py::bytes bytes_value(const std::vector<std::byte> &bytes) {
  auto result = py::bytes(static_cast<const char *>(nullptr), bytes.size());
  auto *target = unpublished_bytes_data(result);
  {
    ReleaseGIL release;
    if (!bytes.empty())
      std::memcpy(target, bytes.data(), bytes.size());
  }
  return result;
}

// Color is the only scene input to worker initialization. Keep it immutable
// while the host captures geometry and the controller opens the device.
py::object worker_startup_snapshot() {
  auto owned = std::make_shared<ch::OwnedSceneSnapshot>();
  owned->color = ch::current_color_configuration();
  owned->portable_color = ch::snapshot_color_policy(owned->color);
  auto pointer = std::make_unique<SnapshotPtr>(std::move(owned));
  auto result = py::capsule(pointer.get(), "cycles_houdini.SceneSnapshot", destroy_owned<SnapshotPtr>);
  if (result)
    pointer.release();
  return result;
}

py::object encode_config(py::handle capture_arg, py::handle desired_arg, py::str root_arg) {
  py::handle capture, desired;
  const char *root;
  capture = capture_arg;
  desired = desired_arg;
  const auto root_storage = root_arg.cast<std::string>();
  root = c_string_checked(root_storage);
  ch::WorkerConfiguration config;
  config.session = handle(capture).config;
  config.session.persistent_updates = true;
  config.color = snapshot(desired)->portable_color;
  config.resource_root = root;
  std::vector<std::byte> bytes;
  {
    ReleaseGIL release;
    bytes = ch::encode_worker_configuration(config);
  }
  return bytes_value(bytes);
}

py::object encode_color_configuration() {
  std::vector<std::byte> bytes;
  {
    ReleaseGIL release;
    bytes = ch::encode_worker_color(ch::snapshot_color_policy(ch::current_color_configuration()));
  }
  return bytes_value(bytes);
}

py::object snapshot_color_identity(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  const auto owned = snapshot(capsule);
  return py::str(owned->portable_color.render_policy_id.c_str());
}

std::atomic<std::uint64_t> next_root_revision{};

NavigationPtr navigation_update(py::handle value) {
  auto *p = capsule_pointer<NavigationPtr>(value, "cycles_houdini.NavigationUpdate");
  if (!p)
    throw std::invalid_argument("Invalid owned navigation update");
  return *p;
}

py::object prepare_navigation(py::handle capsule_arg, py::handle baseline_arg, py::handle data_arg, py::handle time_arg,
                              py::handle previous_arg) {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Navigation acquisition requires Houdini's host thread");
  py::handle capsule, baseline, data, previous = py::none();
  double time;
  capsule = capsule_arg;
  baseline = baseline_arg;
  data = data_arg;
  time = argument_real<double>(time_arg);
  if (!is_omitted(previous_arg))
    previous = previous_arg;
  const auto &h = handle(capsule);
  const auto base = snapshot(baseline);
  if (h.capture_retired)
    throw std::logic_error("Native capture resources were retired");
  const auto &ledger = base->native_navigation;
  const auto motion = item(data, "motion", false) ? motion_settings(item(data, "motion"))
                      : ledger                    ? ledger->motion
                                                  : ch::NativeMotionSettings{};
  if (!h.capture_only || !ledger)
    return py::none();
  const bool matching_context =
      ledger->time == time && ledger->motion == motion &&
      base->portable_color.render_policy_id == ch::presentation_color_configuration()->policy().render_policy_id;
  const bool complete_pose_source =
      item(data, "geometry", false) && (item(data, "camera", false) || item(data, "camera_path", false));
  // Full native captures also freeze poses for a future time/shutter context.
  // navigation_snapshot() will not apply them to an incompatible accepted
  // scene. Keeping the newest overlay prevents a frame revisit from reusing
  // older poses; partial edits must still inherit navigation they do not own.
  if (!matching_context && !complete_pose_source)
    return py::none();
  const auto window = ch::native_motion_window(time, motion);
  auto update = std::make_shared<NavigationUpdate>();
  update->time = time;
  update->motion = motion;
  if (!previous.is_none()) {
    const auto old = navigation_update(previous);
    if (old->time == time && old->motion == motion)
      *update = *old;
  }
  // A complete light update supersedes older transform-only overlays. A
  // later pose preview will rebase against its retained source identities.
  if (item(data, "lights", false))
    update->light_poses.clear();
  ch::SceneUpdate camera;
  prepare_camera_update(camera, data, window, h.width, h.height);
  if (camera.camera)
    update->camera.camera = std::move(camera.camera);
  if (camera.dicing)
    update->camera.dicing = std::move(camera.dicing);
  if (auto value = item(data, "defer_camera_subdivision", false)) {
    if (!py::isinstance<py::bool_>(value))
      throw std::invalid_argument("defer_camera_subdivision requires a boolean");
    update->camera.defer_camera_subdivision = value.is(py::bool_(true));
  }
  if (auto value = item(data, "crop", false)) {
    auto values = sequence(value);
    if (values.size() != 4)
      throw std::invalid_argument("Crop requires x,y,width,height");
    update->camera.crop =
        ch::Crop{int(number(values[0])), int(number(values[1])), int(number(values[2])), int(number(values[3]))};
  }
  if (auto values = item(data, "geometry", false)) {
    update->root_revision = ++next_root_revision;
    update->roots.clear(); // An authoritative root list also retires removed overrides.
    for (auto value : sequence(values)) {
      const auto key = string(item(value, "object_key"));
      auto &root = update->roots[key];
      root = {};
      if (auto v = item(value, "object_path", false))
        root.path = string(v);
      bool blur = true;
      if (auto v = item(value, "motion_blur", false)) {
        if (!py::isinstance<py::bool_>(v))
          throw std::invalid_argument("Object motion_blur must be boolean");
        blur = v.is(py::bool_(true));
      }
      root.window = blur ? window : ch::native_motion_window(time, ch::NativeMotionSettings{});
      if (auto v = item(value, "transform", false))
        root.nominal = matrix(v);
      // A removed/renamed source waits for authoritative capture. Never
      // resurrect a deleted root or dereference its old host node.
      if (!root.path.empty() && !OPgetDirector()->findOBJNode(root.path.c_str())) {
        update->roots.erase(key);
        continue;
      }
      root.centre = root.window.enabled && !root.path.empty()
                        ? ch::capture_object_transform(root.path, root.window.centre)
                        : root.nominal;
      if (root.window.enabled)
        for (const auto sample : root.window.times)
          root.motion.push_back(root.path.empty() ? root.nominal : ch::capture_object_transform(root.path, sample));
    }
  }
  auto pointer = std::make_unique<NavigationPtr>(std::move(update));
  auto result = py::capsule(pointer.get(), "cycles_houdini.NavigationUpdate", destroy_owned<NavigationPtr>);
  if (result)
    pointer.release();
  return result;
}

py::object prepare_object_navigation(py::handle capture_arg, py::handle baseline_arg, py::handle data_arg,
                                     py::handle time_arg, py::handle previous_arg) {
  py::handle capture, baseline, data, previous = py::none();
  double time;
  capture = capture_arg;
  baseline = baseline_arg;
  data = data_arg;
  time = argument_real<double>(time_arg);
  if (!is_omitted(previous_arg))
    previous = previous_arg;
  if (!py::isinstance<py::dict>(data))
    throw std::invalid_argument("Pose preview requires a camera/motion header");
  for (auto [key, value] : py::reinterpret_borrow<py::dict>(data)) {
    const auto name = string(key);
    if (name != "camera" && name != "camera_path" && name != "camera_dof" && name != "camera_sampling" &&
        name != "motion" && name != "pixel_aspect" && name != "dicing" && name != "crop" &&
        name != "defer_camera_subdivision" && name != "light_poses")
      throw std::invalid_argument("Pose preview cannot acquire geometry/material data");
  }
  // The existing camera acquisition enforces the host thread, matching
  // time/shutter/color context and partial-update semantics. No SOP is read.
  py::object camera = prepare_navigation(capture_arg, baseline_arg, data_arg, time_arg, previous_arg);
  if (camera.is_none())
    return py::none();
  const auto base = snapshot(baseline);
  const auto seed = navigation_update(camera);
  auto update = std::make_shared<NavigationUpdate>(*seed);
  for (const auto &[key, root] : base->native_navigation->roots)
    update->roots.try_emplace(key, root);
  const auto determinant = [](const ch::Matrix &m) {
    return double(m[0]) * (double(m[5]) * m[10] - double(m[6]) * m[9]) -
           double(m[1]) * (double(m[4]) * m[10] - double(m[6]) * m[8]) +
           double(m[2]) * (double(m[4]) * m[9] - double(m[5]) * m[8]);
  };
  const auto valid = [&](const ch::Matrix &m) {
    return std::all_of(m.begin(), m.end(), [](float v) { return std::isfinite(v); }) && determinant(m) != 0;
  };
  bool changed = false;
  if (auto poses = item(data, "light_poses", false)) {
    std::map<std::string, NavigationUpdate::LightPose> captured;
    for (auto value : sequence(poses)) {
      auto key = string(item(value, "key"));
      const auto path = string(item(value, "source_path"));
      const auto pose = matrix(item(value, "transform"));
      if (!valid(pose) || !OPgetDirector()->findOBJNode(path.c_str()))
        continue;
      const auto baseline_light = base->state->lights.find(key);
      if (baseline_light == base->state->lights.end() || baseline_light->second.source_path != path)
        continue;
      // A complete authoritative capture can reset a light while an older
      // navigation preparation still retains the same proposed transform.
      // Compare its accepted origin as well as the override itself.
      captured.emplace(std::move(key), NavigationUpdate::LightPose{path, pose, baseline_light->second.transform});
    }
    changed = captured != update->light_poses;
    update->light_poses = std::move(captured);
  }
  for (auto it = update->roots.begin(); it != update->roots.end();) {
    const auto &root = it->second;
    if (root.path.empty()) {
      ++it;
      continue;
    } // Explicit static matrices remain static.
    if (!OPgetDirector()->findOBJNode(root.path.c_str())) {
      it = update->roots.erase(it);
      changed = true;
      continue;
    }
    auto next = root;
    try {
      next.nominal = ch::capture_object_transform(root.path, time);
      next.centre = root.window.enabled ? ch::capture_object_transform(root.path, root.window.centre) : next.nominal;
      next.motion.clear();
      if (root.window.enabled)
        for (double sample : root.window.times)
          next.motion.push_back(ch::capture_object_transform(root.path, sample));
    } catch (const std::invalid_argument &) {
      ++it;
      continue;
    } // Authoritative capture diagnoses missing/evaluation sources.
    catch (const std::runtime_error &) {
      ++it;
      continue;
    }
    const bool negative = determinant(next.centre) < 0;
    if (!valid(next.nominal) || !valid(next.centre) ||
        !std::all_of(next.motion.begin(), next.motion.end(),
                     [&](const auto &m) { return valid(m) && (determinant(m) < 0) == negative; })) {
      ++it;
      continue;
    }
    if (next.nominal != root.nominal || next.centre != root.centre || next.motion != root.motion) {
      it->second = std::move(next);
      changed = true;
    }
    ++it;
  }
  if (update->camera.camera &&
      (!valid(update->camera.camera->transform) ||
       !std::all_of(update->camera.camera->motion.begin(), update->camera.camera->motion.end(), valid)))
    return py::none();
  const auto prior = !previous.is_none() ? navigation_update(previous) : NavigationPtr{};
  const bool same_context = prior && prior->time == time && prior->motion == update->motion;
  const auto &old_camera = same_context && prior->camera.camera ? prior->camera.camera : base->state->camera;
  const auto &crop = update->camera.crop;
  const auto &old_crop = same_context && prior->camera.crop ? prior->camera.crop : base->state->crop;
  const bool old_defer = same_context ? prior->camera.defer_camera_subdivision : base->state->defer_camera_subdivision;
  const bool same_crop = !crop || (old_crop && crop->x == old_crop->x && crop->y == old_crop->y &&
                                   crop->width == old_crop->width && crop->height == old_crop->height);
  if (changed)
    update->root_revision = ++next_root_revision;
  else if (update->camera.camera == old_camera && same_crop && update->camera.defer_camera_subdivision == old_defer)
    return py::none();
  auto pointer = std::make_unique<NavigationPtr>(std::move(update));
  auto result = py::capsule(pointer.get(), "cycles_houdini.NavigationUpdate", destroy_owned<NavigationPtr>);
  if (result)
    pointer.release();
  return result;
}

py::object navigation_info(py::handle capsule_arg) {
  py::handle capsule;
  capsule = capsule_arg;
  const auto nav = navigation_update(capsule);
  return py::dict(
      py::arg("root_overrides") = py::int_(nav->roots.size()), py::arg("root_revision") = py::int_(nav->root_revision),
      py::arg("camera") = py::bool_(nav->camera.camera.has_value()), py::arg("time") = py::float_(nav->time));
}

namespace {
using NavRoot = ch::NativeNavigationState::Root;
ch::Matrix pose_multiply(const ch::Matrix &a, const ch::Matrix &b) {
  ch::Matrix c{};
  for (int row = 0; row < 3; ++row)
    for (int col = 0; col < 4; ++col) {
      for (int k = 0; k < 3; ++k)
        c[row * 4 + col] += a[row * 4 + k] * b[k * 4 + col];
      if (col == 3)
        c[row * 4 + col] += a[row * 4 + 3];
    }
  return c;
}
bool same_window(const ch::NativeMotionWindow &a, const ch::NativeMotionWindow &b) {
  return a.enabled == b.enabled && a.nominal == b.nominal && a.centre == b.centre &&
         a.shutter_frames == b.shutter_frames && a.times == b.times && a.deltas == b.deltas && a.vectors == b.vectors;
}
bool same_root(const NavRoot &a, const NavRoot &b) {
  return a.path == b.path && same_window(a.window, b.window) && a.nominal == b.nominal && a.centre == b.centre &&
         a.motion == b.motion;
}
bool same_roots(const std::map<std::string, NavRoot> &a, const std::map<std::string, NavRoot> &b) {
  return a.size() == b.size() && std::equal(a.begin(), a.end(), b.begin(), [](const auto &x, const auto &y) {
           return x.first == y.first && same_root(x.second, y.second);
         });
}
// Per-instance poses of the origins whose root is in `changed`, from `roots`
// (the host's per-instance path; derive_root_poses repeats this arithmetic).
std::vector<ch::ObjectPoseData> pose_changed_origins(const ch::SceneSnapshotState::ObjectTable &objects,
                                                     const ch::NativeNavigationState::Origins &origins,
                                                     const std::map<std::string, NavRoot> &roots,
                                                     const std::set<std::string> &changed) {
  std::vector<ch::ObjectPoseData> result;
  if (changed.empty())
    return result;
  auto source_object = objects.begin();
  const NavRoot *source_root = nullptr;
  std::string_view source_root_key;
  for (const auto &[key, origin] : origins) {
    if (!changed.contains(origin.root))
      continue;
    while (source_object != objects.end() && source_object->first < key)
      ++source_object;
    const auto found = source_object;
    if (found == objects.end() || found->first != key)
      continue;
    if (!source_root || source_root_key != origin.root) {
      source_root = &roots.at(origin.root);
      source_root_key = origin.root;
    }
    ch::ObjectPoseData object{key};
    const auto &root = *source_root;
    if (root.motion.size() != origin.motion.size())
      throw std::logic_error("Navigation shutter origins do not match root ownership");
    object.transform =
        pose_multiply(root.centre, origin.motion.empty() ? origin.local : origin.motion[origin.motion.size() / 2]);
    object.motion.clear();
    for (std::size_t i = 0; i < origin.motion.size(); ++i)
      object.motion.push_back(pose_multiply(root.motion[i], origin.motion[i]));
    if (std::all_of(object.motion.begin(), object.motion.end(),
                    [&](const auto &pose) { return pose == object.transform; }))
      object.motion.clear();
    if (found->second.nominal_transform)
      object.nominal_transform = pose_multiply(root.nominal, origin.local);
    result.push_back(std::move(object));
  }
  return result;
}
// Roots of `moved` that differ from `baked` (same filter as navigation_snapshot).
std::set<std::string> moved_roots(const ch::NativeNavigationState &baked, const ch::NativeNavigationState &moved) {
  std::set<std::string> changed;
  for (const auto &[key, root] : moved.roots) {
    const auto old = baked.roots.find(key);
    if (old == baked.roots.end() || root.path != old->second.path || !same_window(root.window, old->second.window))
      continue;
    if (root.centre != old->second.centre || root.nominal != old->second.nominal || root.motion != old->second.motion)
      changed.insert(key);
  }
  return changed;
}
bool root_pose_mode() {
  // CYCLES_HOUDINI_ROOT_POSES=0: the per-instance pose path (protocol 90
  // behaviour). Read per preview, so a test can compare both paths in one
  // process; mixed snapshots stay correct (encode materializes them).
  const char *value = std::getenv("CYCLES_HOUDINI_ROOT_POSES");
  return !(value && std::strcmp(value, "0") == 0);
}
struct NavigationMemo {
  std::mutex mutex;
  std::shared_ptr<const ch::SceneSnapshotState::ObjectTable> base, posed;
  std::shared_ptr<const ch::NativeNavigationState> ledger, next;
  std::map<std::string, NavRoot> roots;
  std::atomic<std::uint64_t> hits{}, root_previews{}, materializations{}, root_updates{}, origin_sends{};
};
NavigationMemo &navigation_memo() {
  static NavigationMemo memo;
  return memo;
}
} // namespace

// A root-pose snapshot with its moved roots baked into a private object table
// (the per-instance path). Used where a consumer needs every instance pose.
SnapshotPtr materialize_root_poses(const SnapshotPtr &value) {
  if (!value || !value->baked_navigation)
    return value;
  const auto &baked = *value->baked_navigation;
  const auto &moved = *value->native_navigation;
  if (!baked.origins || baked.origins != moved.origins)
    throw std::logic_error("Root-pose snapshot changed its origins");
  ch::SceneUpdate update;
  update.object_poses =
      pose_changed_origins(*value->state->objects, *moved.origins, moved.roots, moved_roots(baked, moved));
  auto owned = std::make_shared<ch::OwnedSceneSnapshot>(*value);
  owned->state = ch::snapshot_state(value->state, update);
  owned->baked_navigation.reset();
  owned->worker_origin_token.reset();
  owned->worker_origin_roots.reset();
  ++navigation_memo().materializations;
  return owned;
}

py::object navigation_stats() {
  auto &memo = navigation_memo();
  return py::dict(py::arg("memo_hits") = py::int_(memo.hits.load()),
                  py::arg("root_previews") = py::int_(memo.root_previews.load()),
                  py::arg("materializations") = py::int_(memo.materializations.load()),
                  py::arg("root_updates") = py::int_(memo.root_updates.load()),
                  py::arg("origin_sends") = py::int_(memo.origin_sends.load()),
                  py::arg("root_pose_mode") = py::bool_(root_pose_mode()));
}

py::object release_navigation_memo() {
  auto &memo = navigation_memo();
  std::shared_ptr<const ch::SceneSnapshotState::ObjectTable> base, posed;
  std::shared_ptr<const ch::NativeNavigationState> ledger, next;
  {
    const std::lock_guard lock(memo.mutex);
    base.swap(memo.base);
    posed.swap(memo.posed);
    ledger.swap(memo.ledger);
    next.swap(memo.next);
    memo.roots.clear();
  }
  {
    ReleaseGIL release; // Tables can be large: free them without the GIL.
    base.reset();
    posed.reset();
    ledger.reset();
    next.reset();
  }
  return py::none();
}

py::object navigation_snapshot(py::handle capsule_arg, py::handle navigation_arg, py::handle keep_unchanged_arg,
                               py::handle camera_only_arg) {
  py::handle capsule, navigation;
  int keep_unchanged = 0, camera_only = 0;
  capsule = capsule_arg;
  navigation = navigation_arg;
  keep_unchanged = truth(keep_unchanged_arg);
  camera_only = truth(camera_only_arg);
  const auto base = snapshot(capsule);
  const auto nav = navigation_update(navigation);
  if (!base->native_navigation || base->native_navigation->time != nav->time ||
      base->native_navigation->motion != nav->motion)
    return py::none();
  SnapshotPtr result;
  {
    ReleaseGIL release;
    const auto &ledger = *base->native_navigation;
    if (!ledger.origins || !base->state->objects)
      throw std::logic_error("Navigation snapshot has no owned object provenance");
    auto update = nav->camera;
    if (!camera_only)
      for (const auto &[key, pose] : nav->light_poses) {
        const auto found = base->state->lights.find(key);
        // Never resurrect deleted/replaced sources or overwrite a new
        // shader/shape with the values from an older owned preparation.
        if (found == base->state->lights.end() || found->second.source_path != pose.path ||
            found->second.transform == pose.transform)
          continue;
        auto light = found->second;
        light.transform = pose.transform;
        update.lights.push_back(std::move(light));
      }
    auto next = std::make_shared<ch::NativeNavigationState>(ledger);
    std::set<std::string> changed;
    if (!camera_only)
      for (const auto &[key, root] : nav->roots) {
        const auto old = ledger.roots.find(key);
        if (old == ledger.roots.end() || root.path != old->second.path || !same_window(root.window, old->second.window))
          continue;
        if (root.centre != old->second.centre || root.nominal != old->second.nominal ||
            root.motion != old->second.motion) {
          next->roots[key] = root;
          changed.insert(key);
        }
      }
    auto &memo = navigation_memo();
    // Root mode (protocol 91): keep the accepted object table and record only
    // the moved roots; encode_snapshot sends their matrices and the renderer
    // derives the instance poses. No per-instance work on the host.
    const bool roots_only = root_pose_mode() && !changed.empty();
    // Per-instance mode: one-entry memo of the posed object table (a pure
    // function of the base table, the base ledger and the root values).
    // CYCLES_HOUDINI_POSED_TABLE_MEMO=0 switches it off.
    static const bool memo_enabled = [] {
      const char *value = std::getenv("CYCLES_HOUDINI_POSED_TABLE_MEMO");
      return !(value && std::strcmp(value, "0") == 0);
    }();
    std::shared_ptr<const ch::SceneSnapshotState::ObjectTable> posed_table;
    std::shared_ptr<const ch::NativeNavigationState> posed_next;
    if (!roots_only && memo_enabled && !changed.empty()) {
      const std::lock_guard lock(memo.mutex);
      if (memo.posed && memo.base == base->state->objects && memo.ledger == base->native_navigation &&
          same_roots(memo.roots, next->roots)) {
        posed_table = memo.posed;
        posed_next = memo.next;
        ++memo.hits;
      }
    }
    if (!roots_only && !posed_table && !changed.empty()) {
      auto poses = pose_changed_origins(*base->state->objects, *ledger.origins, next->roots, changed);
      for (auto &pose : poses)
        update.object_poses.push_back(std::move(pose));
    }
    if (update.camera == base->state->camera)
      update.camera.reset();
    if (update.crop && base->state->crop) {
      const auto &a = *update.crop;
      const auto &b = *base->state->crop;
      if (a.x == b.x && a.y == b.y && a.width == b.width && a.height == b.height)
        update.crop.reset();
    }
    if (!roots_only && !posed_table && update.object_poses.empty() && update.lights.empty() && !update.camera &&
        !update.crop && update.defer_camera_subdivision == base->state->defer_camera_subdivision) {
      if (keep_unchanged) {
        auto owned = std::make_shared<ch::OwnedSceneSnapshot>(*base);
        owned->reuse_if_unchanged = true;
        result = std::move(owned);
      }
    } else if (roots_only) {
      auto owned = std::make_shared<ch::OwnedSceneSnapshot>(*base);
      owned->state = ch::snapshot_state(base->state, update); // Shares the object table.
      owned->baked_navigation = base->baked_navigation ? base->baked_navigation : base->native_navigation;
      owned->native_navigation = next;
      owned->reuse_if_unchanged = true;
      ++memo.root_previews;
      result = std::move(owned);
    } else if (posed_table) {
      auto owned = std::make_shared<ch::OwnedSceneSnapshot>(*base);
      owned->state = ch::snapshot_state(base->state, update, posed_table);
      owned->native_navigation = posed_next;
      owned->reuse_if_unchanged = true;
      result = std::move(owned);
    } else {
      auto owned = std::make_shared<ch::OwnedSceneSnapshot>(*base);
      owned->state = ch::snapshot_state(base->state, update);
      owned->native_navigation = changed.empty() ? base->native_navigation : next;
      owned->reuse_if_unchanged = true;
      if (memo_enabled && !changed.empty() && !update.object_poses.empty()) {
        const std::lock_guard lock(memo.mutex);
        memo.base = base->state->objects;
        memo.ledger = base->native_navigation;
        memo.roots = next->roots;
        memo.posed = owned->state->objects;
        memo.next = next;
      }
      result = std::move(owned);
    }
  }
  if (!result)
    return py::none();
  auto pointer = std::make_unique<SnapshotPtr>(std::move(result));
  auto value = py::capsule(pointer.get(), "cycles_houdini.SceneSnapshot", destroy_owned<SnapshotPtr>);
  if (value)
    pointer.release();
  return value;
}

py::object navigation_basis_equal(py::handle before_arg, py::handle after_arg) {
  py::handle before, after;
  before = before_arg;
  after = after_arg;
  const auto a = snapshot(before), b = snapshot(after);
  const bool same = a->native_navigation && b->native_navigation &&
                    a->native_navigation->origins == b->native_navigation->origins &&
                    a->state->objects == b->state->objects && a->state->meshes == b->state->meshes &&
                    a->state->points == b->state->points && a->state->curves == b->state->curves &&
                    a->state->volumes == b->state->volumes && a->state->lights == b->state->lights;
  return py::bool_(same);
}

py::object navigation_context_equal(py::handle capsule_arg, py::handle navigation_arg) {
  py::handle capsule, navigation;
  capsule = capsule_arg;
  navigation = navigation_arg;
  const auto base = snapshot(capsule);
  const auto nav = navigation_update(navigation);
  return py::bool_(base->native_navigation && base->native_navigation->time == nav->time &&
                   base->native_navigation->motion == nav->motion);
}

py::object snapshot_info(py::handle capsule_arg, py::handle ack_arg) {
  py::handle capsule, ack = py::none();
  capsule = capsule_arg;
  if (!is_omitted(ack_arg))
    ack = ack_arg;
  // Test/diagnostic reader: counts and differences of the instance poses as
  // rendered, so root-pose snapshots are materialized first.
  const auto owned = materialize_root_poses(snapshot(capsule));
  const auto value = owned->state;
  std::uint64_t vertices{}, triangles{}, points{}, curve_keys{}, curves{}, fields{}, pointiness_vertices{},
      mesh_motion_samples{};
  for (const auto &[key, mesh] : value->meshes) {
    vertices += mesh->positions.size();
    triangles += mesh->triangles.size();
    pointiness_vertices += mesh->pointiness.size();
    mesh_motion_samples += mesh->motion.positions.size();
  }
  for (const auto &[key, p] : value->points)
    points += p->positions.size();
  for (const auto &[key, c] : value->curves) {
    curve_keys += c->positions.size();
    curves += c->first_key.size();
  }
  for (const auto &[key, v] : value->volumes)
    fields += v->fields.size();
  ch::WorkerUpdate images;
  const auto previous = ack.is_none() ? SnapshotPtr{} : materialize_root_poses(snapshot(ack));
  ch::texture_image_difference(previous ? previous->textures.get() : nullptr, *owned->textures, images);
  std::uint64_t pixel_bytes{};
  for (const auto &image : images.image_upserts)
    pixel_bytes += image->pixels.size() * sizeof(float);
  py::dict result = py::dict(
      py::arg("mesh_vertices") = py::int_(vertices), py::arg("mesh_triangles") = py::int_(triangles),
      py::arg("points") = py::int_(points), py::arg("curves") = py::int_(curves),
      py::arg("curve_keys") = py::int_(curve_keys), py::arg("volume_fields") = py::int_(fields),
      py::arg("geometry_resources") =
          py::int_(value->meshes.size() + value->volumes.size() + value->points.size() + value->curves.size()),
      py::arg("objects") = py::int_(value->objects->size()), py::arg("materials") = py::int_(value->materials.size()),
      py::arg("image_resources") = py::int_(owned->textures->images.size()),
      py::arg("image_upserts") = py::int_(images.image_upserts.size()),
      py::arg("image_deletions") = py::int_(images.deleted_images.size()),
      py::arg("image_pixel_bytes") = py::int_(pixel_bytes));
  py::int_ intrinsic = py::int_(pointiness_vertices);
  result["pointiness_vertices"] = intrinsic;
  std::uint64_t instance_fields{}, sampled_instance_fields{}, incomplete_instance_fields{}, instance_samples{},
      object_motion_samples{};
  for (const auto &[key, object] : *value->objects) {
    object_motion_samples += object.motion.size();
    for (const auto &field : object.attributes) {
      ++instance_fields;
      sampled_instance_fields += !field.motion_values.empty();
      incomplete_instance_fields += !field.motion_complete;
      instance_samples += field.motion_values.size();
    }
  }
  for (const auto &[name, count] :
       {std::pair{"instance_attribute_fields", instance_fields},
        std::pair{"sampled_instance_attribute_fields", sampled_instance_fields},
        std::pair{"incomplete_instance_attribute_fields", incomplete_instance_fields},
        std::pair{"instance_attribute_sample_values", instance_samples},
        std::pair{"object_motion_samples", object_motion_samples},
        std::pair{"mesh_motion_samples", mesh_motion_samples},
        std::pair{"camera_motion_samples", std::uint64_t(value->camera ? value->camera->motion.size() : 0)},
        std::pair{"camera_dof_samples", std::uint64_t(value->camera ? value->camera->dof_motion.size() : 0)},
        std::pair{"camera_intrinsic_samples",
                  std::uint64_t(value->camera ? value->camera->intrinsic_motion.size() : 0)}}) {
    py::int_ field_count = py::int_(count);
    result[name] = field_count;
  }
  if (previous) {
    ch::SceneUpdate delta;
    {
      ReleaseGIL release;
      delta = ch::snapshot_difference(previous->state.get(), *value);
    }
    py::dict difference = py::dict(py::arg("mesh_upserts") = py::int_(delta.meshes.size()),
                                   py::arg("point_upserts") = py::int_(delta.points.size()),
                                   py::arg("curve_upserts") = py::int_(delta.curves.size()),
                                   py::arg("volume_upserts") = py::int_(delta.volumes.size()),
                                   py::arg("object_upserts") = py::int_(delta.objects.size()),
                                   py::arg("object_poses") = py::int_(delta.object_poses.size()),
                                   py::arg("object_attributes") = py::int_(delta.object_attributes.size()),
                                   py::arg("object_attribute_poses") = py::int_(
                                       std::count_if(delta.object_attributes.begin(), delta.object_attributes.end(),
                                                     [](const auto &command) { return bool(command.pose); })),
                                   py::arg("object_deletions") = py::int_(delta.deleted_objects.size()),
                                   py::arg("geometry_deletions") = py::int_(delta.deleted_geometry.size()),
                                   py::arg("material_upserts") = py::int_(delta.materials.size()));
    result["difference"] = difference;
  }
  return result;
}

struct SnapshotDigestHash {
  std::uint64_t value = 1469598103934665603ull;
  void bytes(const void *data, std::size_t size) {
    const auto *p = static_cast<const unsigned char *>(data);
    for (std::size_t n = 0; n < size; ++n) {
      value ^= p[n];
      value *= 1099511628211ull;
    }
  }
  template <class T> void pod(const T &v) { bytes(&v, sizeof(v)); }
  void text(const std::string &v) {
    pod(v.size());
    bytes(v.data(), v.size());
  }
  void matrix(const ch::Matrix &m) { bytes(m.data(), sizeof(float) * m.size()); }
  void matrices(const std::vector<ch::Matrix> &values) {
    pod(values.size());
    for (const auto &m : values)
      matrix(m);
  }
};
// Test hook: content digests of a snapshot's object table and navigation
// origins, for comparing two conversion paths. Every field, bit-exact.
py::object snapshot_digest(py::handle capsule) {
  const auto owned = materialize_root_poses(snapshot(capsule)); // Bit-exact digest of the poses as rendered.
  SnapshotDigestHash objects, poses, navigation;
  {
    ReleaseGIL release;
    for (const auto &[key, object] : *owned->state->objects) {
      auto &h = objects;
      h.text(key);
      h.text(object.key);
      h.text(object.geometry_key);
      h.pod(object.visibility);
      h.pod(object.random_id);
      const auto &p = object.properties;
      for (const bool flag : {p.holdout, p.caustics_caster, p.caustics_receiver, p.shadow_catcher})
        h.pod(flag);
      for (const float value : {p.shadow_terminator_shading, p.shadow_terminator_geometry, p.ao_distance, p.alpha})
        h.pod(value);
      for (const auto *text : {&p.lightgroup, &p.cryptomatte_object, &p.cryptomatte_asset, &p.linking_id})
        h.text(*text);
      h.pod(p.pass_index);
      h.pod(p.dielectric_priority);
      h.pod(p.color);
      h.pod(object.attributes.size());
      for (const auto &field : object.attributes) {
        h.text(field.name);
        h.pod(field.components);
        h.pod(field.value);
        h.pod(field.motion_complete);
        h.pod(field.motion_values.size());
        for (const auto &sample : field.motion_values)
          h.pod(sample);
      }
      poses.text(key);
      poses.matrix(object.transform);
      poses.matrices(object.motion);
      poses.pod(bool(object.nominal_transform));
      if (object.nominal_transform)
        poses.matrix(*object.nominal_transform);
    }
    if (const auto &state = owned->native_navigation) {
      navigation.pod(state->time);
      for (const auto &[key, root] : state->roots) {
        navigation.text(key);
        navigation.text(root.path);
        navigation.matrix(root.nominal);
        navigation.matrix(root.centre);
        navigation.matrices(root.motion);
      }
      if (state->origins)
        for (const auto &[key, origin] : *state->origins) {
          navigation.text(key);
          navigation.text(origin.root);
          navigation.matrix(origin.local);
          navigation.matrices(origin.motion);
        }
    }
  }
  const auto hex = [](std::uint64_t value) {
    char text[17];
    std::snprintf(text, sizeof(text), "%016llx", static_cast<unsigned long long>(value));
    return std::string(text);
  };
  std::size_t geometry = owned->state->meshes.size() + owned->state->points.size() + owned->state->curves.size() +
                         owned->state->volumes.size();
  return py::dict(py::arg("objects") = py::int_(owned->state->objects->size()),
                  py::arg("object_digest") = hex(objects.value), py::arg("pose_digest") = hex(poses.value),
                  py::arg("navigation_digest") = hex(navigation.value),
                  py::arg("origins") = py::int_(owned->native_navigation && owned->native_navigation->origins
                                                    ? owned->native_navigation->origins->size()
                                                    : 0),
                  py::arg("geometry") = py::int_(geometry));
}

// B1 transfer. encode_snapshot returns a WirePacket: the encoded update whose
// large arrays are written only once, on all cores, by write_into() straight
// into the renderer's reused input mapping. Python code that reads the packet
// as bytes (len, buffer, ==, bytes()) gets exactly encode_worker_update's
// bytes, built once on first use. CYCLES_HOUDINI_WIRE_LEGACY=1 returns bytes
// from the previous path instead (A/B timing and the equality test).
struct WirePacket {
  ch::GatheredUpdate gathered;
  std::mutex mutex;
  std::shared_ptr<const std::vector<std::byte>> bytes;
  std::shared_ptr<const std::vector<std::byte>> materialize() {
    ReleaseGIL release;
    std::lock_guard lock(mutex);
    if (!bytes) {
      auto value = std::make_shared<std::vector<std::byte>>(std::size_t(gathered.size()));
      gathered.write(value->data());
      bytes = std::move(value);
    }
    return bytes;
  }
};
bool wire_legacy() {
  const auto *value = std::getenv("CYCLES_HOUDINI_WIRE_LEGACY");
  return value && std::string_view(value) == "1";
}
void bind_wire_transport(py::module_ &m) {
  py::class_<WirePacket, std::shared_ptr<WirePacket>>(m, "WirePacket", py::buffer_protocol(), py::module_local())
      .def_buffer([](WirePacket &packet) {
        const auto bytes = packet.materialize(); // The packet keeps them for the buffer's lifetime.
        return py::buffer_info(const_cast<std::byte *>(bytes->data()), 1,
                               py::format_descriptor<unsigned char>::format(), 1, {py::ssize_t(bytes->size())}, {1},
                               true);
      })
      .def("__len__", [](const WirePacket &packet) { return std::size_t(packet.gathered.size()); })
      .def("__bytes__",
           [](WirePacket &packet) {
             const auto bytes = packet.materialize();
             return py::bytes(reinterpret_cast<const char *>(bytes->data()), bytes->size());
           })
      .def("__eq__",
           [](WirePacket &packet, py::handle other) -> py::object {
             if (!py::isinstance<py::buffer>(other))
               return py::reinterpret_borrow<py::object>(Py_NotImplemented);
             const auto mine = packet.materialize();
             ReadBuffer buffer;
             buffer.request(other);
             const auto size = std::size_t(buffer.value.size * buffer.value.itemsize);
             return py::bool_(size == mine->size() &&
                              (!size || std::memcmp(buffer.value.ptr, mine->data(), size) == 0));
           })
      .def("write_into", [](WirePacket &packet, py::buffer target) {
        // target: the writable input mapping. Its owner (ExternalBackend) never
        // rewrites it while the renderer may read it.
        const auto info = target.request(true);
        if (info.ndim != 1 || info.itemsize != 1 || info.strides[0] != 1 ||
            std::uint64_t(info.size) < packet.gathered.size())
          throw std::invalid_argument("Renderer input mapping is too small for the packet");
        ReleaseGIL release;
        packet.gathered.write(static_cast<std::byte *>(info.ptr));
      });
}

namespace {
struct OriginToken {
  std::shared_ptr<const void> origins, table; // Identity only; kept alive with the token.
  std::uint64_t id{};
};
std::atomic<std::uint64_t> next_origin_token{0};
} // namespace

// Protocol 91. Root-pose snapshots that share the acknowledged object table and
// baked ledger send their moved roots; the renderer receives each root's
// origins once per (origins, table). Anything else is materialized first and
// encoded per instance, as before. Bookkeeping lives on the desired snapshot.
void prepare_root_poses(SnapshotPtr &desired, SnapshotPtr &previous, ch::RootPoseUpdate &out) {
  const auto target = desired; // The caller's snapshot carries the bookkeeping.
  target->worker_origin_token.reset();
  target->worker_origin_roots.reset();
  const auto effective = [](const SnapshotPtr &value) {
    return value->baked_navigation ? value->baked_navigation : value->native_navigation;
  };
  const bool shared = previous && desired->native_navigation && previous->native_navigation &&
                      desired->state->objects == previous->state->objects && effective(desired) &&
                      effective(desired) == effective(previous) &&
                      desired->native_navigation->origins == previous->native_navigation->origins &&
                      desired->native_navigation->origins;
  if (!shared || (!desired->baked_navigation && !previous->baked_navigation)) {
    desired = materialize_root_poses(desired);
    previous = materialize_root_poses(previous);
    return;
  }
  const auto &want = *desired->native_navigation;
  const auto &have = *previous->native_navigation;
  std::vector<std::string> send;
  for (const auto &[key, root] : want.roots) {
    const auto old = have.roots.find(key);
    if (old == have.roots.end() || !same_root(root, old->second))
      send.push_back(key);
  }
  for (const auto &[key, root] : have.roots)
    if (!want.roots.contains(key))
      throw std::logic_error("Root-pose snapshot lost a root of its ledger");
  std::shared_ptr<const OriginToken> token;
  std::set<std::string> known;
  if (const auto prior = std::static_pointer_cast<const OriginToken>(previous->worker_origin_token);
      prior && prior->origins == want.origins && prior->table == desired->state->objects) {
    token = prior;
    if (previous->worker_origin_roots)
      known = *previous->worker_origin_roots;
  }
  const bool reset = !token;
  if (reset) {
    auto fresh = std::make_shared<OriginToken>();
    fresh->origins = want.origins;
    fresh->table = desired->state->objects;
    fresh->id = ++next_origin_token;
    token = fresh;
  }
  std::set<std::string> missing;
  for (const auto &key : send)
    if (!known.contains(key))
      missing.insert(key);
  if (!missing.empty()) {
    const auto &objects = *desired->state->objects;
    auto object = objects.begin();
    for (const auto &[key, origin] : *want.origins) {
      if (!missing.contains(origin.root))
        continue;
      while (object != objects.end() && object->first < key)
        ++object;
      if (object == objects.end() || object->first != key)
        continue; // As navigation_snapshot: origins without an object are not posed.
      out.origins.push_back({key, origin.root, origin.local, origin.motion, bool(object->second.nominal_transform)});
    }
    std::stable_sort(out.origins.begin(), out.origins.end(),
                     [](const auto &a, const auto &b) { return a.root < b.root; });
    known.insert(missing.begin(), missing.end());
    ++navigation_memo().origin_sends;
  }
  for (const auto &key : send) {
    const auto &root = want.roots.at(key);
    out.roots.push_back({key, root.centre, root.nominal, root.motion});
  }
  out.token = token->id;
  out.reset_origins = reset;
  // Roots with no instance in the table derive nothing; the renderer still
  // needs an (empty) origin list for them.
  if (!missing.empty())
    for (const auto &key : missing)
      if (std::none_of(out.origins.begin(), out.origins.end(), [&](const auto &o) { return o.root == key; }))
        out.origins.push_back({"", key, {}, {}, false});
  if (!out.roots.empty())
    ++navigation_memo().root_updates;
  target->worker_origin_token = token;
  target->worker_origin_roots = std::make_shared<const std::set<std::string>>(std::move(known));
}

py::object encode_snapshot(py::handle desired_arg, py::handle ack_arg, py::handle reuse_mesh_payloads_arg,
                           py::handle root_poses_arg) {
  const bool root_poses = truth(root_poses_arg);
  py::handle desired, ack = py::none();
  int reuse_mesh_payloads = 1;
  desired = desired_arg;
  if (!is_omitted(ack_arg))
    ack = ack_arg;
  reuse_mesh_payloads = truth(reuse_mesh_payloads_arg);
  auto owned = snapshot(desired), previous = ack.is_none() ? SnapshotPtr{} : snapshot(ack);
  if (!wire_legacy()) {
    auto packet = std::make_shared<WirePacket>();
    {
      ReleaseGIL release;
      auto update = std::make_shared<ch::WorkerUpdate>();
      if (root_poses) {
        prepare_root_poses(owned, previous, update->root_poses);
      } else {
        // Per-instance fallback (renderer refused a root update): the worker's
        // origins are no longer trusted, so the next root update resets them.
        owned->worker_origin_token.reset();
        owned->worker_origin_roots.reset();
        owned = materialize_root_poses(owned);
        previous = materialize_root_poses(previous);
      }
      update->scene = ch::snapshot_difference(previous ? previous->state.get() : nullptr, *owned->state);
      update->textures = *owned->textures;
      update->color = owned->portable_color;
      update->reuse_if_unchanged = owned->reuse_if_unchanged;
      update->textures.images.clear();
      ch::texture_image_difference(previous ? previous->textures.get() : nullptr, *owned->textures, *update);
      packet->gathered = ch::encode_worker_update_gathered(
          std::move(update), previous ? &previous->state->meshes : nullptr, reuse_mesh_payloads != 0);
    }
    return py::cast(std::move(packet));
  }
  std::vector<std::byte> bytes;
  {
    ReleaseGIL release;
    owned = materialize_root_poses(owned);
    previous = materialize_root_poses(previous);
    snapshot(desired)->worker_origin_token.reset();
    ch::WorkerUpdate update;
    update.scene = ch::snapshot_difference(previous ? previous->state.get() : nullptr, *owned->state);
    update.textures = *owned->textures;
    update.color = owned->portable_color;
    update.reuse_if_unchanged = owned->reuse_if_unchanged;
    update.textures.images.clear();
    ch::texture_image_difference(previous ? previous->textures.get() : nullptr, *owned->textures, update);
    bytes = ch::encode_worker_update(update, previous ? &previous->state->meshes : nullptr, reuse_mesh_payloads != 0);
  }
  return bytes_value(bytes);
}

py::dict reply_value(py::handle packet, const std::shared_ptr<const ch::ColorConfiguration> &color) {
  ReadBuffer buffer;
  buffer.request(packet);
  ch::WorkerReply reply;
  {
    ReleaseGIL release;
    reply = ch::decode_worker_reply(
        {static_cast<const std::byte *>(buffer.value.ptr), std::size_t(buffer.value.size * buffer.value.itemsize)},
        color);
  }
  if (reply.focus_is_frame && (!reply.frame || reply.focus_frame))
    throw std::invalid_argument("Invalid worker focus-frame alias");
  py::object focus = frame_capsule(reply.focus_is_frame ? reply.frame : std::move(reply.focus_frame));
  py::object s = status_value(reply.status);
  py::object r = rendering_value(reply.status.rendering);
  py::object d = diagnostics_value(reply.diagnostics);
  py::object t = texture_inputs_value(reply.textures);
  py::object f = frame_capsule(std::move(reply.frame));
  s["rendering"] = r;
  py::list device_list = py::list(0);
  for (const auto &device : reply.devices) {
    py::list denoisers = py::list(0);
    for (const auto &name : device.denoisers) {
      py::str token = py::str(name.c_str());
      denoisers.append(token);
    }
    py::dict value =
        py::dict(py::arg("id") = py::str(device.id.c_str()), py::arg("name") = py::str(device.name.c_str()),
                 py::arg("backend") = py::str(device.backend.c_str()), py::arg("denoisers") = denoisers,
                 py::arg("physical_id") = py::str(device.physical_id.c_str()));
    device_list.append(value);
  }
  return py::dict(py::arg("success") = py::bool_(reply.success), py::arg("error") = py::str(reply.error.c_str()),
                  py::arg("update_rejected") = py::bool_(reply.update_rejected),
                  py::arg("renderer_pid") = py::int_(reply.renderer_pid), py::arg("status") = s,
                  py::arg("diagnostics") = d, py::arg("texture_inputs") = t,
                  py::arg("textures_changed") = py::bool_(reply.textures_changed), py::arg("frame") = f,
                  py::arg("process_memory") = py::str(reply.process_memory.c_str()), py::arg("focus_frame") = focus,
                  py::arg("devices") = device_list);
}

py::object decode_reply(py::handle packet_arg, py::handle desired_arg) {
  py::handle packet, desired;
  packet = packet_arg;
  desired = desired_arg;
  return reply_value(packet, snapshot(desired)->color);
}

py::object frame_receiver() {
  auto value = std::make_unique<FrameReceiver>(ch::current_color_configuration());
  auto result = py::capsule(value.get(), "cycles_houdini.FrameReceiver", destroy_owned<FrameReceiver>);
  if (result)
    value.release();
  return result;
}

py::object decode_render_reply(py::handle packet_arg, py::handle receiver_arg) {
  py::handle packet, receiver;
  packet = packet_arg;
  receiver = receiver_arg;
  const auto *color = capsule_pointer<FrameReceiver>(receiver, "cycles_houdini.FrameReceiver");
  if (!color)
    throw std::invalid_argument("Invalid final-render frame receiver");
  return reply_value(packet, *color);
}

py::object encode_render_reply(py::handle capsule_arg, py::handle after_arg) {
  py::handle capsule;
  unsigned long long after = 0;
  capsule = capsule_arg;
  after = unsigned_argument_mask(after_arg);
  auto &h = handle(capsule);
  if (!h.session)
    throw std::logic_error("Final display requires a rendering session");
  std::vector<std::byte> bytes;
  {
    ReleaseGIL release;
    ch::WorkerReply reply;
    reply.status = h.session->status();
    reply.frame = h.session->frame(after);
    reply.textures = h.session->texture_inputs();
    if (reply.frame && reply.frame->generation != reply.status.generation)
      reply.frame.reset();
    bytes = ch::encode_worker_reply(reply);
  }
  return bytes_value(bytes);
}
} // namespace cycles_houdini::python_detail
