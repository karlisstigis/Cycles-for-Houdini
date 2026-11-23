// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "python_binding_detail.h"
#include "scene_adapter.h"
#include "tile_display.h"
#include "cop_images.h"
#include "cycles_houdini/snapshot.h"
#include <condition_variable>
#include <map>
#include <mutex>
#include <optional>
#include <set>
#include <thread>
#include <utility>

namespace cycles_houdini::python_detail {
py::dict status_value(const ch::SessionStatus &status, const ch::ExtractionStats &stats = {});

struct PolicySlot {
  mutable std::mutex mutex;
  std::shared_ptr<const ch::OwnedTexturePolicy> value;
  std::shared_ptr<const ch::OwnedTexturePolicy> get() const {
    std::lock_guard lock(mutex);
    return value;
  }
  std::shared_ptr<const ch::OwnedTexturePolicy> replace(std::shared_ptr<const ch::OwnedTexturePolicy> next) {
    std::lock_guard lock(mutex);
    return std::exchange(value, std::move(next));
  }
};

struct Handle {
  // Protects admission, display replacement, and the binding-owned state read
  // by status. Never hold this across RenderSession calls or Python callbacks.
  mutable std::mutex state_mutex;
  std::condition_variable state_changed;
  std::thread::id closing_thread;
  bool closing{}, closed{};
  std::unique_ptr<ch::RenderSession> session;
  std::shared_ptr<ch::TileDisplay> display;
  ch::NativeSceneAdapter adapter;
  ch::ExtractionStats published_stats;
  ch::NativePointInstanceCache::Counters published_point_instances;
  std::vector<std::string> accepted_material_paths, accepted_stylesheet_resources;
  int width{}, height{};
  bool applying{}, capture_only{}, capture_retired{};
  ch::SessionConfig config;
  std::shared_ptr<PolicySlot> policy_slot;
  std::unique_ptr<ch::SnapshotTextureResolver> texture_resolver;
  std::shared_ptr<const ch::SceneSnapshotState> desired;
  // One prior committed snapshot stays owned until the next converter/scan.
  // Replacing h.desired must not free a large object map on the UI thread.
  std::shared_ptr<const ch::SceneSnapshotState> retired_snapshot;
  std::shared_ptr<const ch::SceneSnapshotState> texture_state;
  ch::NativeCopImageResolver cop_images;
  std::vector<ch::SourceDiagnostic> captured_source_diagnostics;
  std::set<std::string> native_material_keys;
  std::vector<ch::NativeMaterialReference> material_references;
  // prepare_material_edit -> the next materials-only apply against the same committed state: the edited
  // materials' graphs, resolved once; every other native graph stays as committed.
  struct MaterialEdit {
    std::shared_ptr<const ch::SceneSnapshotState> base;
    double time{};
    std::map<std::string, ch::MaterialData> graphs;
  };
  std::optional<MaterialEdit> material_edit;

  void begin_apply() {
    std::lock_guard lock(state_mutex);
    if (closing || closed)
      throw std::logic_error("Native session is closing");
    if (capture_retired)
      throw std::logic_error("Native capture resources were retired");
    if (applying)
      throw std::logic_error("A native scene transaction cannot be reentered");
    published_stats = capture_stats(*this);
    published_point_instances = adapter.point_instances().counters();
    applying = true;
  }
  void end_apply() {
    std::lock_guard lock(state_mutex);
    applying = false;
  }
};

struct ApplyGuard {
  Handle &h;
  explicit ApplyGuard(Handle &value) : h(value) { h.begin_apply(); }
  ~ApplyGuard() { h.end_apply(); }
};

} // namespace cycles_houdini::python_detail
