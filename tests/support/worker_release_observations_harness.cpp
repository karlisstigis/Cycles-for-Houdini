// SPDX-License-Identifier: Apache-2.0
// Real public types: API drift must fail compilation rather than silently
// continue exercising a copied observer implementation.
#include "cycles_houdini/snapshot.h"
#include "cycles_houdini/memory.h"
#include "cycles_houdini/ipc/control_pipe.h"
#include "cycles_houdini/ipc/shared_memory.h"
#include "platform.h"
#include <chrono>
#include <cstring>
#include <iostream>
#include <locale>
#include <sstream>
#include <stdexcept>
#include <cstdlib>
#include <deque>

// @WORKER_MAIN@

namespace fixture {
using namespace cycles_houdini;
void require(bool condition, const char *message) {
  if (!condition)
    throw std::runtime_error(message);
}
std::deque<ipc::Message> requests;
std::vector<WorkerReply> replies;
WorkerReply encoded;
std::uint16_t active{};
std::shared_ptr<FrameEvent> beauty, focus;
bool ready{}, changed{}, alias_focus{}, during_status{};
int released{}, frame_reads{}, focus_reads{}, texture_reads{};

void complete() {
  ready = changed = true;
  beauty = std::make_shared<FrameEvent>();
  beauty->generation = 7;
  beauty->sequence = 101;
  beauty->complete = true;
  beauty->width = beauty->height = beauty->full_width = beauty->full_height = 1;
  beauty->planes.push_back({"combined", 4, {0.125f, 0.25f, 0.5f, 1.0f}, 1, 1});
  focus = alias_focus ? beauty : std::make_shared<FrameEvent>(*beauty);
  if (!alias_focus)
    focus->sequence = 202;
}
void reset(bool alias, bool status) {
  requests.clear();
  replies.clear();
  beauty.reset();
  focus.reset();
  encoded = {};
  ready = changed = false;
  alias_focus = alias;
  during_status = status;
  released = frame_reads = focus_reads = texture_reads = 0;
  // The reused input mapping really enters the production cache on init.
  requests.push_back({1, 1, 1, std::string(ipc::reused_input_prefix) + "fixture"});
  for (std::uint64_t i = 2; i <= 6; ++i)
    requests.push_back({std::uint16_t(i == 2 || i == 3 ? 14 : i == 6 ? 7 : 3), i, 0, {}});
}
} // namespace fixture

namespace cycles_houdini::ipc {
struct SharedMemory::Impl {
  std::string name;
  std::vector<std::byte> bytes;
};
SharedMemory::SharedMemory() : impl_(std::make_unique<Impl>()) {}
SharedMemory::~SharedMemory() {
  if (impl_->name.starts_with(reused_input_prefix)) {
    ++fixture::released;
    if (!fixture::during_status)
      fixture::complete();
  }
}
void SharedMemory::open(const std::string &name, std::uint64_t size) { create(name, size); }
std::uint64_t SharedMemory::open_whole(const std::string &name) {
  create(name, 1);
  return 1;
}
void SharedMemory::create(const std::string &name, std::uint64_t size) {
  impl_->name = name;
  impl_->bytes.resize(std::size_t(size));
}
void *SharedMemory::data() const noexcept { return impl_->bytes.data(); }
struct WorkerControlPipe::Impl {
  bool ack{};
  std::uint64_t request{};
};
WorkerControlPipe::WorkerControlPipe() : impl_(std::make_unique<Impl>()) {}
WorkerControlPipe::~WorkerControlPipe() = default;
bool WorkerControlPipe::read(Message &message) {
  if (impl_->ack) {
    message = {acknowledgement_command, impl_->request, 0, {}};
    impl_->ack = false;
    return true;
  }
  if (fixture::requests.empty())
    return false;
  message = fixture::requests.front();
  fixture::requests.pop_front();
  fixture::active = message.command;
  return true;
}
void WorkerControlPipe::send(const Message &message) {
  fixture::require(message.command == (fixture::active | 0x8000), "wrong reply command");
  fixture::replies.push_back(fixture::encoded);
  impl_->ack = true;
  impl_->request = message.request;
}
void WorkerControlPipe::write_smoke_noise() {}
} // namespace cycles_houdini::ipc
namespace cycles_houdini::platform {
void watch_parent_process(std::uint32_t) {}
std::uint32_t process_id() noexcept { return 123; }
std::uint64_t uptime_milliseconds() noexcept { return 456; }
bool environment_flag(const char *) { return false; }
} // namespace cycles_houdini::platform
namespace cycles_houdini {
struct RenderSession::Impl {};
RenderSession::RenderSession(const SessionConfig &) : impl_(std::make_unique<Impl>()) {}
RenderSession::~RenderSession() = default;
SessionStatus RenderSession::status() const {
  if (fixture::active == 14 && fixture::during_status && !fixture::ready)
    fixture::complete();
  SessionStatus result;
  result.generation = 7;
  result.state = fixture::ready ? SessionState::Complete : SessionState::Rendering;
  return result;
}
SessionDiagnostics RenderSession::diagnostics() const { return {}; }
std::shared_ptr<const FrameEvent> RenderSession::frame(std::uint64_t after) const {
  if (fixture::active == 14)
    ++fixture::frame_reads;
  return fixture::ready && after < fixture::beauty->sequence ? fixture::beauty : nullptr;
}
std::shared_ptr<const FrameEvent> RenderSession::focus_frame(std::uint64_t after) const {
  if (fixture::active == 14)
    ++fixture::focus_reads;
  return fixture::ready && after < fixture::focus->sequence ? fixture::focus : nullptr;
}
std::vector<TextureInputInfo> RenderSession::texture_inputs() const { return {}; }
bool RenderSession::textures_changed() const {
  if (fixture::active == 14)
    ++fixture::texture_reads;
  const bool result = fixture::changed;
  fixture::changed = false;
  return result;
}
std::uint64_t RenderSession::apply(const SceneUpdate &, bool) { return 7; }
void RenderSession::close() {}
void RenderSession::cancel() {}
void RenderSession::pause(bool) {}
void RenderSession::prepare_color_configuration_change() {}
void RenderSession::set_preview_pass(const std::string &) {}
void RenderSession::set_preview(const PreviewRequest &) {}
void RenderSession::write_exr(const std::string &) const {}
void RenderSession::write_exr(const std::string &, const OutputOptions &) const {}
MeshPayloadSources RenderSession::retained_mesh_sources() const { return {}; }
void enable_renderer_device_probes() {}
void initialize(const std::string &, const std::string &) {}
Capabilities capabilities() { return {}; }
std::string process_memory_json() { return "{}"; }
bool synchronize_color_configuration() { return false; }
void activate_snapshot_color_policy(const OwnedColorPolicy &) {}
TextureInputPolicy snapshot_texture_policy(const std::shared_ptr<const OwnedTexturePolicy> &) { return {}; }
WorkerConfiguration decode_worker_configuration(std::span<const std::byte>) { return {}; }
WorkerUpdate decode_worker_update(std::span<const std::byte>, const MeshPayloadSources *, std::uint64_t) { return {}; }
std::vector<ObjectPoseData> derive_root_poses(const std::map<std::string, std::vector<PoseOriginRecord>> &,
                                              const std::vector<RootPoseRecord> &) {
  throw std::logic_error("Root pose derivation is outside the worker observation fixture");
}
WorkerOutput decode_worker_output(std::span<const std::byte>) { return {}; }
PreviewRequest decode_worker_preview(std::span<const std::byte>) { return {}; }
OwnedColorPolicy decode_worker_color(std::span<const std::byte>) { return {}; }
MeshPayloadSources mesh_payload_sources(const MeshPayloadSources &previous, const SceneUpdate &) { return previous; }
std::shared_ptr<const OwnedTexturePolicy>
apply_texture_image_difference(const std::shared_ptr<const OwnedTexturePolicy> &previous, WorkerUpdate &) {
  return previous;
}
std::shared_ptr<const SceneSnapshotState>
shader_resource_state(const std::shared_ptr<const SceneSnapshotState> &previous, const SceneUpdate &) {
  return previous;
}
void validate_texture_image_references(const SceneSnapshotState &, const OwnedTexturePolicy &) {}
std::vector<std::byte> encode_worker_reply(const WorkerReply &reply) {
  fixture::encoded = reply;
  return {std::byte{0}};
}
} // namespace cycles_houdini

int main() {
  try {
    for (bool alias : {false, true})
      for (bool during_status : {false, true}) {
        fixture::reset(alias, during_status);
        char program[] = "worker", option[] = "--parent-pid", pid[] = "123";
        char *argv[] = {program, option, pid};
        fixture::require(worker_main(3, argv) == 0, "worker command loop failed");
        using fixture::require;
        require(fixture::released == 1, "command 14 did not release cached input mapping exactly once");
        require(fixture::replies.size() == 6, "missing command replies");
        for (const auto &reply : fixture::replies)
          require(reply.success, "worker returned failure");
        const auto &poll = fixture::replies[3];
        require(poll.frame && poll.frame->complete && poll.frame->sequence == 101 && poll.frame->generation == 7,
                "first poll lost completed frame");
        for (int index : {1, 2}) {
          const auto &release = fixture::replies[std::size_t(index)];
          require(release.status.state == cycles_houdini::SessionState::Complete,
                  "final frame not ready at input release");
          require(!release.frame && !release.focus_frame && !release.focus_is_frame && !release.textures_changed,
                  "discarded command 14 reply consumed one-time observations");
        }
        require(fixture::frame_reads == 0 && fixture::focus_reads == 0 && fixture::texture_reads == 0,
                "command 14 observed consumable renderer state");
        require(poll.frame->planes[0].pixels == std::vector<float>({0.125f, 0.25f, 0.5f, 1.0f}),
                "final pixels changed");
        require(poll.textures_changed, "first poll lost texture notification");
        require(alias ? poll.focus_is_frame && !poll.focus_frame
                      : !poll.focus_is_frame && poll.focus_frame && poll.focus_frame->sequence == 202,
                "first poll lost focus frame or alias");
        const auto &again = fixture::replies[4];
        require(!again.frame && !again.focus_frame && !again.focus_is_frame && !again.textures_changed,
                "poll delivered one-time observations twice");
      }
    std::cout << "{\"scenarios\":4,\"release_replies\":8,\"polls\":8,\"result\":\"passed\"}\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
