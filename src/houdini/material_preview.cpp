// SPDX-License-Identifier: Apache-2.0
#include "material_preview.h"
#include "cycles_houdini/volume.h"
#include <OP/OP_Director.h>
#include <OP/OP_Operator.h>
#include <VOP/VOP_Node.h>
#include <TIL/TIL_Thumbnail.h>
#include <TIL/TIL_Raster.h>
#include <UT/UT_Thread.h>
#include <algorithm>
#include <cmath>
#include <cstring>
#include <deque>
#include <map>
#include <mutex>

namespace cycles_houdini {
namespace {
constexpr std::size_t queue_limit = 16, image_limit = 32, byte_limit = 8 * 1024 * 1024;
struct State {
  long long node{};
  std::uint64_t revision{};
  int size{128};
  bool pending{}, alive{true};
  int percent{};
  std::string error;
  std::unique_ptr<TIL_Raster> raster;
};
struct Mailbox {
  std::mutex mutex;
  std::map<long long, std::weak_ptr<State>> states;
  std::deque<std::shared_ptr<State>> pending;
  std::deque<std::weak_ptr<State>> images;
  MaterialPreviewStats stats;
};
// Host VOPs can outlive extension/GUI shutdown callbacks. Keep the small
// mailbox alive until process exit; it owns no renderer, worker or host node.
Mailbox &mailbox() {
  static auto *value = new Mailbox;
  return *value;
}
std::size_t bytes(const State &state) {
  return state.raster ? std::size_t(state.raster->getXres()) * state.raster->getYres() * 4 : 0;
}
void compact(Mailbox &box) {
  std::erase_if(box.states, [](const auto &entry) { return entry.second.expired(); });
  std::erase_if(box.images, [](const auto &entry) {
    const auto state = entry.lock();
    return !state || !state->raster;
  });
  box.stats.image_bytes = box.stats.images = 0;
  for (const auto &weak : box.images)
    if (const auto state = weak.lock(); state && state->raster) {
      box.stats.image_bytes += bytes(*state);
      ++box.stats.images;
    }
}
MaterialPreviewRequest enqueue(const std::shared_ptr<State> &state, int size) {
  auto &box = mailbox();
  std::lock_guard lock(box.mutex);
  compact(box);
  state->size = std::clamp(size, 32, 256);
  ++state->revision;
  state->error.clear();
  state->percent = 0;
  if (state->pending) {
    ++box.stats.coalesced;
    std::erase(box.pending, state);
  }
  state->pending = true;
  box.pending.push_back(state);
  while (box.pending.size() > queue_limit) {
    auto old = box.pending.front();
    box.pending.pop_front();
    old->pending = false;
    old->error = "Preview queue full; request this material again";
    old->percent = 100;
    ++box.stats.evicted;
  }
  return {state->node, state->revision, state->size};
}
class Thumbnail final : public TIL_Thumbnail {
public:
  explicit Thumbnail(std::shared_ptr<State> state) : state_(std::move(state)) {}
  bool owns(const std::shared_ptr<State> &state) const { return state_ == state; }
  void start(const TIL_ThumbnailParms &parms) override { enqueue(state_, parms.res()); }
  int percentComplete() const override {
    auto &box = mailbox();
    std::lock_guard lock(box.mutex);
    return state_->percent;
  }
  UT_StringHolder errorMessage() const override {
    auto &box = mailbox();
    std::lock_guard lock(box.mutex);
    return UT_StringHolder(state_->error);
  }
  const TIL_Raster *getImage() const override {
    auto &box = mailbox();
    std::lock_guard lock(box.mutex);
    return state_->raster.get();
  }

private:
  std::shared_ptr<State> state_;
};
void host_thread() {
  if (!UT_Thread::isMainThread())
    throw std::logic_error("Material thumbnail publication requires the host thread");
}
} // namespace
class MaterialThumbnailOwner {
public:
  std::shared_ptr<State> state = std::make_shared<State>();
  ~MaterialThumbnailOwner() {
    auto &box = mailbox();
    std::lock_guard lock(box.mutex);
    state->alive = false;
    ++state->revision;
    std::erase(box.pending, state);
    box.states.erase(state->node);
    state->raster.reset();
    compact(box);
  }
};
MaterialThumbnailLease install_material_thumbnail(VOP_Node &node) {
  auto owner = std::make_shared<MaterialThumbnailOwner>();
  owner->state->node = node.getUniqueId();
  {
    auto &box = mailbox();
    std::lock_guard lock(box.mutex);
    compact(box);
    box.states[owner->state->node] = owner->state;
  }
  node.setThumbnail(new Thumbnail(owner->state));
  return owner;
}
void ensure_material_thumbnail(VOP_Node &node, const MaterialThumbnailLease &owner) {
  if (!owner)
    return;
  const auto *current = dynamic_cast<Thumbnail *>(node.getThumbnail());
  if (!current || !current->owns(owner->state))
    node.setThumbnail(new Thumbnail(owner->state));
}
MaterialPreviewRequest request_material_thumbnail(long long id, int size) {
  host_thread();
  auto *node = dynamic_cast<VOP_Node *>(OP_Node::lookupNode(id));
  if (!node || (node->getOperator()->getName() != "cycles::material::1" &&
                node->getOperator()->getName() != "cycles::volume_material::1" &&
                node->getOperator()->getName() != "cycles::world_material::1" &&
                node->getOperator()->getName() != "cycles::light_material::1"))
    throw std::invalid_argument("Choose a Cycles surface, volume or world material for this preview");
  std::shared_ptr<State> state;
  {
    auto &box = mailbox();
    std::lock_guard lock(box.mutex);
    state = box.states.at(id).lock();
  }
  if (!state)
    throw std::logic_error("Missing native Cycles thumbnail owner");
  if (!dynamic_cast<Thumbnail *>(node->getThumbnail()))
    node->setThumbnail(new Thumbnail(state));
  return enqueue(state, size);
}
std::vector<MaterialPreviewRequest> take_material_previews() {
  host_thread();
  auto &box = mailbox();
  std::lock_guard lock(box.mutex);
  std::vector<MaterialPreviewRequest> result;
  for (const auto &state : box.pending) {
    state->pending = false;
    result.push_back({state->node, state->revision, state->size});
  }
  box.pending.clear();
  return result;
}
bool material_preview_current(const MaterialPreviewRequest &request) {
  auto &box = mailbox();
  std::lock_guard lock(box.mutex);
  const auto found = box.states.find(request.node);
  const auto state = found == box.states.end() ? std::shared_ptr<State>{} : found->second.lock();
  return state && state->alive && state->revision == request.revision && state->size == request.size;
}
bool publish_material_thumbnail(const MaterialPreviewRequest &request, std::span<const std::byte> rgba,
                                const std::string &error) {
  host_thread();
  auto &box = mailbox();
  {
    std::lock_guard lock(box.mutex);
    compact(box);
    const auto found = box.states.find(request.node);
    const auto state = found == box.states.end() ? std::shared_ptr<State>{} : found->second.lock();
    if (!state || !state->alive || state->revision != request.revision || state->size != request.size) {
      ++box.stats.stale;
      return false;
    }
    if (error.empty()) {
      if (rgba.size() != std::size_t(request.size) * request.size * 4)
        throw std::invalid_argument("Invalid native thumbnail pixel count");
      auto image = std::make_unique<TIL_Raster>(PACK_RGBA, TILE_INT8, request.size, request.size);
      // The Cycles viewer buffer has top-left rows; TIL uses bottom-left.
      auto *destination = static_cast<std::byte *>(image->getPixels());
      const auto row = std::size_t(request.size) * 4;
      for (int y = 0; y < request.size; ++y)
        std::memcpy(destination + std::size_t(y) * row, rgba.data() + std::size_t(request.size - 1 - y) * row, row);
      state->raster = std::move(image);
      ++box.stats.published;
      std::erase_if(box.images, [&](const auto &entry) { return entry.lock() == state; });
      box.images.push_back(state);
      compact(box);
      while (box.stats.images > image_limit || box.stats.image_bytes > byte_limit) {
        auto oldest = box.images.front().lock();
        box.images.pop_front();
        if (oldest)
          oldest->raster.reset();
        compact(box);
      }
    } else
      state->raster.reset();
    state->error = error;
    state->percent = 100;
  }
  // Notify the thumbnail consumer, not the shader's dependency system.
  if (auto *node = dynamic_cast<VOP_Node *>(OP_Node::lookupNode(request.node))) {
    std::shared_ptr<State> state;
    {
      std::lock_guard lock(box.mutex);
      state = box.states.at(request.node).lock();
    }
    // A fresh handle also clears the host's one-shot needsRefresh flag.
    node->setThumbnail(new Thumbnail(state));
    node->getThumbnail()->update(TIL_Thumbnail::TIL_THUMB_DONE);
  }
  return true;
}
MaterialPreviewStats material_preview_stats() {
  auto &box = mailbox();
  std::lock_guard lock(box.mutex);
  compact(box);
  auto result = box.stats;
  result.queued = box.pending.size();
  return result;
}
std::vector<std::byte> material_thumbnail_pixels(long long id) {
  // Inspect the actual host-facing interface, not a separate plugin cache.
  host_thread();
  const auto *node = dynamic_cast<VOP_Node *>(OP_Node::lookupNode(id));
  const auto *thumbnail = node ? dynamic_cast<Thumbnail *>(node->getThumbnail()) : nullptr;
  const auto *image = thumbnail ? thumbnail->getImage() : nullptr;
  if (!image)
    return {};
  const auto *begin = static_cast<const std::byte *>(image->getPixels());
  return {begin, begin + std::size_t(image->getXres()) * image->getYres() * 4};
}
SceneUpdate material_preview_fixture(std::string_view domain, bool world_medium) {
  if (domain != "surface" && domain != "volume" && domain != "world")
    throw std::invalid_argument("Unknown material preview domain");
  if (world_medium && domain != "world")
    throw std::invalid_argument("A world-medium reference requires the world preview domain");
  SceneUpdate scene;
  scene.camera = CameraData{};
  scene.camera->transform[11] = 3.7f;
  scene.camera->short_axis_fov = .72f;
  // An absent update means "retain" to the persistent engine. Explicitly
  // clear the previous domain's environment and transparency state.
  scene.world = WorldData{};
  scene.transparent_background = false;
  scene.background = Float3{.12f, .12f, .12f};
  scene.materials.push_back({"preview/subject", MaterialKind::Diffuse, {.5f, .5f, .5f}, .4f});
  // Infinite absorption can extinguish the infinitely distant environment.
  // A finite emissive reference makes its attenuation visible in a swatch.
  if (world_medium)
    scene.materials.front().kind = MaterialKind::Emission;
  if (domain == "volume") {
    openvdb::initialize();
    auto density = openvdb::FloatGrid::create(0.f);
    density->setName("density");
    density->setGridClass(openvdb::GRID_FOG_VOLUME);
    density->setTransform(openvdb::math::Transform::createLinearTransform(1. / 16));
    auto temperature = openvdb::FloatGrid::create(0.f);
    temperature->setName("temperature");
    temperature->setTransform(density->transform().copy());
    auto color = openvdb::Vec3SGrid::create(openvdb::Vec3s(0));
    color->setName("color");
    color->setTransform(density->transform().copy());
    auto d = density->getAccessor();
    auto t = temperature->getAccessor();
    auto c = color->getAccessor();
    for (int z = -15; z <= 15; ++z)
      for (int y = -15; y <= 15; ++y)
        for (int x = -15; x <= 15; ++x) {
          const float radius = std::sqrt(float(x * x + y * y + z * z)) / 16;
          if (radius >= .95f)
            continue;
          const float value = std::min(1.f, (.95f - radius) * 8);
          const openvdb::Coord coord(x, y, z);
          d.setValue(coord, value);
          t.setValue(coord, 1200 + 800 * (1 - radius));
          c.setValue(coord, openvdb::Vec3s(1));
        }
    auto volume = std::make_shared<VolumeData>();
    volume->key = "preview/cloud";
    volume->material = "preview/subject";
    volume->revision = 1;
    volume->recover_material = true;
    volume->fields = {{"density", density}, {"temperature", temperature}, {"color", color}};
    scene.volumes.push_back(volume);
    scene.objects.push_back({"preview/cloud", volume->key});
    auto &material = scene.materials.front();
    material.kind = MaterialKind::PrincipledVolume;
    material.color = {.5f, .5f, .5f};
    material.density = 1;
  } else {
    auto mesh = std::make_shared<MeshData>();
    mesh->key = "preview/sphere";
    mesh->revision = 1;
    mesh->smooth = true;
    mesh->materials = {"preview/subject"};
    constexpr int rows = 32, columns = 64;
    constexpr float pi = 3.14159265358979323846f;
    for (int y = 0; y <= rows; ++y)
      for (int x = 0; x <= columns; ++x) {
        const float latitude = pi * float(y) / rows, longitude = 2 * pi * float(x) / columns;
        mesh->positions.push_back(
            {std::sin(latitude) * std::cos(longitude), std::cos(latitude), std::sin(latitude) * std::sin(longitude)});
      }
    const auto add = [&](int a, int b, int c) {
      mesh->triangles.push_back({a, b, c});
      for (const int i : {a, b, c}) {
        mesh->corner_normals.push_back(mesh->positions[i]);
        mesh->corner_uv.push_back({float(i % (columns + 1)) / columns, 1 - float(i / (columns + 1)) / rows});
        mesh->corner_colors.push_back({1, 1, 1});
      }
    };
    for (int y = 0; y < rows; ++y)
      for (int x = 0; x < columns; ++x) {
        const int a = y * (columns + 1) + x, b = a + columns + 1;
        if (y > 0)
          add(a, a + 1, b);
        if (y < rows - 1)
          add(a + 1, b + 1, b);
      }
    scene.meshes.push_back(mesh);
    scene.objects.push_back({"preview/sphere", mesh->key});
  }
  if (domain != "world")
    for (const auto &spec : {std::array<float, 4>{-3, 4, 4, 500}, std::array<float, 4>{3, 1, 2, 150}}) {
      LightData light;
      light.key = "preview/light" + std::to_string(scene.lights.size());
      light.transform[3] = spec[0];
      light.transform[7] = spec[1];
      light.transform[11] = spec[2];
      light.radius = 1;
      light.strength = {spec[3], spec[3], spec[3]};
      scene.lights.push_back(light);
    }
  return scene;
}
} // namespace cycles_houdini
