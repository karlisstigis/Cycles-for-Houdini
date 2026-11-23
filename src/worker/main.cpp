// SPDX-License-Identifier: Apache-2.0
// Experimental persistent native renderer. stdout is a binary control pipe;
// bulk scene/output data uses acknowledged named shared-memory mappings.
#include "cycles_houdini/snapshot.h"
#include "cycles_houdini/memory.h"
#include "../core/color_internal.h"
#include "../platform/platform.h"
#include "cycles_houdini/ipc/control_pipe.h"
#include "cycles_houdini/ipc/shared_memory.h"
#include <algorithm>
#include <array>
#include <iterator>
#include <map>
#include <set>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <locale>
#include <sstream>
#include <thread>

namespace ch = cycles_houdini;
namespace {
using ch::ipc::Message;
using Mapping = ch::ipc::SharedMemory;
constexpr auto ack_command = ch::ipc::acknowledgement_command;
using Clock = std::chrono::steady_clock;
double elapsed(Clock::time_point start) { return std::chrono::duration<double>(Clock::now() - start).count(); }
struct ScopedTiming {
  double *result;
  Clock::time_point start;
  explicit ScopedTiming(double *value) : result(value) {
    if (result)
      start = Clock::now();
  }
  ~ScopedTiming() {
    if (result)
      *result += elapsed(start);
  }
};

} // namespace
int main(int argc, char **argv) {
  try {
    ch::ipc::WorkerControlPipe pipe;
    if (argc == 2 && std::string(argv[1]) == "--transport-smoke") {
      ch::ipc::WorkerControlPipe::write_smoke_noise();
      pipe.send({std::uint16_t(3 | 0x8000), 41, 0, {}});
      return 0;
    }
    if (argc != 3 || std::string(argv[1]) != "--parent-pid")
      throw std::invalid_argument("Expected --parent-pid <Houdini PID>");
    ch::platform::watch_parent_process(static_cast<std::uint32_t>(std::stoul(argv[2])));
    ch::enable_renderer_device_probes();
    std::unique_ptr<ch::RenderSession> session;
    std::shared_ptr<const ch::OwnedTexturePolicy> textures = std::make_shared<const ch::OwnedTexturePolicy>();
    auto image_state = std::make_shared<const ch::SceneSnapshotState>();
    ch::MeshPayloadSources mesh_state;
    // Protocol 91: origins of gesture root moves, per root, sorted by key.
    std::map<std::string, std::vector<ch::PoseOriginRecord>> pose_origins;
    std::uint64_t pose_origin_token{};
    const bool profiling = ch::platform::environment_flag("CYCLES_HOUDINI_PROFILE_WORKER");
    std::string color_identity;
    std::uint64_t last_frame{}, last_focus{};
    bool closing = false;
    bool disk_only = false;
    std::vector<ch::DeviceDescriptor> worker_devices;
    // B1: the view of Houdini's reused input mapping stays open between
    // requests (no remapping, no soft faults again). It is read only while a
    // request is handled; command 14 drops it before Houdini frees the mapping.
    std::unique_ptr<Mapping> reused_input;
    std::string reused_name;
    std::uint64_t reused_size{};
    Message input;
    while (pipe.read(input)) {
      ch::WorkerReply reply;
      reply.renderer_pid = ch::platform::process_id();
      const bool profile = profiling && input.command == 2;
      double mapping_seconds{}, decode_seconds{}, ledger_seconds{}, policy_seconds{}, apply_seconds{},
          observation_seconds{}, root_pose_seconds{};
      std::size_t root_poses{};
      std::size_t object_records{}, object_poses{}, object_attributes{};
      try {
        if (input.command == ack_command)
          throw std::invalid_argument("Unexpected renderer acknowledgement");
        Mapping source;
        std::span<const std::byte> payload;
        if (input.size) {
          ScopedTiming timer(profile ? &mapping_seconds : nullptr);
          if (input.mapping.starts_with(ch::ipc::reused_input_prefix)) {
            if (!reused_input || reused_name != input.mapping) {
              reused_input.reset();
              reused_name.clear();
              auto view = std::make_unique<Mapping>();
              reused_size = view->open_whole(input.mapping);
              reused_input = std::move(view);
              reused_name = input.mapping;
            }
            if (input.size > reused_size)
              throw std::invalid_argument("Invalid input mapping size");
            payload = {static_cast<const std::byte *>(reused_input->data()), std::size_t(input.size)};
          } else {
            source.open(input.mapping, input.size);
            payload = {static_cast<const std::byte *>(source.data()), std::size_t(input.size)};
          }
        }
        if (input.command == 1 || input.command == 13) {
          disk_only = input.command == 13;
          if (session)
            throw std::logic_error("Renderer is already initialized");
          auto config = ch::decode_worker_configuration(payload);
          ch::activate_snapshot_color_policy(config.color);
          color_identity = config.color.render_policy_id;
          ch::initialize(config.resource_root);
          config.session.persistent_updates = true;
          worker_devices = ch::capabilities().devices;
          config.session.texture_input_policy.identity = [&textures]() { return textures->identity; };
          config.session.texture_input_policy.resolve_auto = [&textures](const ch::TextureHeader &h) {
            return ch::snapshot_texture_policy(textures).resolve_auto(h);
          };
          config.session.texture_input_policy.resolve_image = [&textures](const std::string &source, double time) {
            return ch::snapshot_texture_policy(textures).resolve_image(source, time);
          };
          session = std::make_unique<ch::RenderSession>(config.session);
        } else {
          if (!session)
            throw std::logic_error("Renderer is not initialized");
          switch (input.command) {
          case 2: {
            ch::WorkerUpdate update;
            {
              ScopedTiming timer(profile ? &decode_seconds : nullptr);
              update = ch::decode_worker_update(payload, &mesh_state);
            }
            if (update.root_poses.token)
              try {
                ScopedTiming timer(profile ? &root_pose_seconds : nullptr);
                auto &roots = update.root_poses;
                if (const char *refuse = std::getenv("CYCLES_HOUDINI_TEST_REFUSE_ROOT_POSES");
                    refuse && std::strcmp(refuse, "1") == 0 && !roots.roots.empty())
                  throw std::invalid_argument("refused by CYCLES_HOUDINI_TEST_REFUSE_ROOT_POSES=1 (test hook)");
                if (roots.reset_origins) {
                  pose_origins.clear();
                  pose_origin_token = roots.token;
                }
                if (roots.token != pose_origin_token)
                  throw std::invalid_argument("Root poses refer to origins the renderer does not hold");
                std::set<std::string> replaced;
                for (auto &origin : roots.origins) {
                  if (replaced.insert(origin.root).second)
                    pose_origins[origin.root].clear();
                  if (!origin.key.empty()) // An empty key only declares a root without instances.
                    pose_origins[origin.root].push_back(std::move(origin));
                }
                for (const auto &key : replaced) {
                  auto &list = pose_origins[key];
                  std::sort(list.begin(), list.end(), [](const auto &a, const auto &b) { return a.key < b.key; });
                }
                roots.origins.clear();
                if (!roots.roots.empty()) {
                  auto derived = ch::derive_root_poses(pose_origins, roots.roots);
                  root_poses = derived.size();
                  auto &poses = update.scene.object_poses;
                  const char *fast = std::getenv("CYCLES_HOUDINI_POSE_FAST");
                  if (poses.empty() && !(fast && std::strcmp(fast, "0") == 0)) {
                    poses = std::move(derived); // Sorted and unique already (derive_root_poses).
                  } else {
                    std::vector<ch::ObjectPoseData> merged;
                    merged.reserve(poses.size() + derived.size());
                    std::sort(poses.begin(), poses.end(), [](const auto &a, const auto &b) { return a.key < b.key; });
                    std::merge(std::make_move_iterator(poses.begin()), std::make_move_iterator(poses.end()),
                               std::make_move_iterator(derived.begin()), std::make_move_iterator(derived.end()),
                               std::back_inserter(merged), [](const auto &a, const auto &b) { return a.key < b.key; });
                    for (std::size_t n = 1; n < merged.size(); ++n)
                      if (merged[n - 1].key == merged[n].key)
                        throw std::invalid_argument("Object pose sent twice (root and explicit): " + merged[n].key);
                    poses = std::move(merged);
                  }
                }
              } catch (const std::exception &error) {
                // Before any scene transaction: keep the renderer and the accepted
                // scene; the host retries this update with every instance pose.
                pose_origins.clear();
                pose_origin_token = 0;
                throw ch::SceneUpdateRejected(std::string("Root pose update refused: ") + error.what());
              }
            const bool mesh_sources_changed = !update.scene.meshes.empty() || !update.scene.deleted_geometry.empty();
            if (mesh_sources_changed) {
              ScopedTiming timer(profile ? &ledger_seconds : nullptr);
              ch::mesh_payload_sources(mesh_state, update.scene);
            }
            object_records = update.scene.objects.size();
            object_poses = update.scene.object_poses.size();
            object_attributes = update.scene.object_attributes.size();
            const auto previous_textures = textures;
            auto candidate_image_state = image_state;
            {
              ScopedTiming timer(profile ? &policy_seconds : nullptr);
              if (color_identity != update.color.render_policy_id) {
                session->prepare_color_configuration_change();
                ch::activate_snapshot_color_policy(update.color);
                color_identity = update.color.render_policy_id;
              }
              auto candidate = ch::apply_texture_image_difference(previous_textures, update);
              candidate_image_state = ch::shader_resource_state(image_state, update.scene);
              ch::validate_texture_image_references(*candidate_image_state, *candidate);
              textures = std::move(candidate);
            }
            try {
              ScopedTiming timer(profile ? &apply_seconds : nullptr);
              session->apply(update.scene, update.reuse_if_unchanged);
            } catch (...) {
              textures = previous_textures;
              throw;
            }
            image_state = std::move(candidate_image_state);
            if (mesh_sources_changed)
              mesh_state = session->retained_mesh_sources();
            break;
          }
          case 3:
            if (input.size)
              throw std::invalid_argument("Poll has no payload");
            break;
          case ch::ipc::release_input_command:
            if (input.size)
              throw std::invalid_argument("Input release has no payload");
            reused_input.reset();
            reused_name.clear();
            reused_size = 0;
            break;
          case 4:
            if (payload.size() != 1 || std::to_integer<unsigned char>(payload[0]) > 1)
              throw std::invalid_argument("Invalid pause request");
            session->pause(payload[0] != std::byte{});
            break;
          case 5:
            session->cancel();
            break;
          case 6: {
            const std::string path(reinterpret_cast<const char *>(payload.data()), payload.size());
            if (path.empty() || path.find('\0') != std::string::npos)
              throw std::invalid_argument("Invalid output path");
            session->write_exr(path);
            break;
          }
          case 11: {
            const auto output = ch::decode_worker_output(payload);
            session->write_exr(output.filename, output.options);
            break;
          }
          case 7:
            session->close();
            mesh_state.clear();
            closing = true;
            break;
          case 8:
            reply.process_memory = ch::process_memory_json();
            break;
          case 9: {
            const std::string pass(reinterpret_cast<const char *>(payload.data()), payload.size());
            if (pass.empty() || pass.size() > 256 || pass.find('\0') != std::string::npos)
              throw std::invalid_argument("Invalid preview pass");
            session->set_preview_pass(pass);
            break;
          }
          case 12:
            session->set_preview(ch::decode_worker_preview(payload));
            break;
          case 10: {
            const auto color = ch::decode_worker_color(payload);
            if (color_identity != color.render_policy_id) {
              session->prepare_color_configuration_change();
              ch::activate_snapshot_color_policy(color);
              ch::synchronize_color_configuration();
              color_identity = color.render_policy_id;
            }
            break;
          }
          default:
            throw std::invalid_argument("Unknown renderer command");
          }
        }
        ScopedTiming observer(profile ? &observation_seconds : nullptr);
        reply.status = session->status();
        reply.diagnostics = session->diagnostics();
        // Houdini discards the reply to an input release. It must not consume the
        // one-time observations below (a new frame, focus frame, texture change):
        // a frame that completed just then would never reach the viewer.
        if (!closing && input.command != ch::ipc::release_input_command) {
          reply.textures = session->texture_inputs();
          reply.textures_changed = session->textures_changed();
          if (disk_only) {
            // Never map display buffers into Houdini for disk output. Report
            // completion only after the writer can consume this generation.
            if (reply.status.state == ch::SessionState::Complete) {
              const auto frame = session->frame();
              if (!frame || !frame->complete || frame->generation != reply.status.generation)
                reply.status.state = ch::SessionState::Rendering;
            }
          } else {
            auto frame = session->frame(last_frame);
            if (frame && frame->generation == reply.status.generation) {
              last_frame = frame->sequence;
              reply.frame = std::move(frame);
            }
            auto focus = session->focus_frame(last_focus);
            if (focus && focus->generation == reply.status.generation) {
              last_focus = focus->sequence;
              if (focus == reply.frame)
                reply.focus_is_frame = true;
              else
                reply.focus_frame = std::move(focus);
            }
          }
        }
      } catch (const ch::SceneUpdateRejected &error) {
        reply.success = false;
        reply.update_rejected = true;
        reply.error = error.what();
        std::cerr << reply.error << '\n';
        // No scene transaction began. Keep the renderer and its GPU
        // resources; the host must retain its previous snapshot ACK.
        // A render-color policy carried by this update stays active:
        // the host only sends snapshots matching its current policy,
        // so rolling back would disagree with every later update. If
        // the policy changed, the retained scene waits (Idle, color
        // update pending) for the next accepted update to re-render.
        reply.status = session->status();
        reply.diagnostics = session->diagnostics();
        reply.textures = session->texture_inputs();
        reply.textures_changed = session->textures_changed();
        // The reply is decoded with the candidate's color policy.
        // Preserve the host's previous image; normal polls use the
        // last accepted policy and may publish further old-scene tiles.
      } catch (const std::exception &error) {
        reply.success = false;
        reply.error = error.what();
        std::cerr << reply.error << '\n';
        if (session)
          reply.status = session->status();
      }
      reply.devices = worker_devices;
      const auto encode_start = profile ? Clock::now() : Clock::time_point{};
      const auto bytes = ch::encode_worker_reply(reply);
      const double encode_seconds = profile ? elapsed(encode_start) : 0;
      const auto name = "Local\\CyclesHoudiniReply-" + std::to_string(ch::platform::process_id()) + "-" +
                        std::to_string(ch::platform::uptime_milliseconds()) + "-" + std::to_string(input.request);
      const auto output_start = profile ? Clock::now() : Clock::time_point{};
      Mapping target;
      target.create(name, bytes.size());
      std::memcpy(target.data(), bytes.data(), bytes.size());
      const double output_mapping_seconds = profile ? elapsed(output_start) : 0;
      pipe.send({std::uint16_t(input.command | 0x8000), input.request, bytes.size(), name});
      if (profile) {
        // Opt-in per-request observations, outside the versioned scene
        // protocol. No filenames or scene content enter the log record.
        std::ostringstream record;
        record.imbue(std::locale::classic());
        record << "CH_PROFILE {\"request\":" << input.request << ",\"input_bytes\":" << input.size
               << ",\"output_bytes\":" << bytes.size() << ",\"success\":" << (reply.success ? "true" : "false")
               << ",\"object_records\":" << object_records << ",\"object_poses\":" << object_poses
               << ",\"object_attributes\":" << object_attributes << ",\"root_poses\":" << root_poses
               << ",\"root_pose_seconds\":" << root_pose_seconds << ",\"input_mapping_seconds\":" << mapping_seconds
               << ",\"decode_seconds\":" << decode_seconds << ",\"ledger_seconds\":" << ledger_seconds
               << ",\"policy_seconds\":" << policy_seconds << ",\"apply_seconds\":" << apply_seconds
               << ",\"observation_seconds\":" << observation_seconds << ",\"reply_encode_seconds\":" << encode_seconds
               << ",\"output_mapping_seconds\":" << output_mapping_seconds << "}";
        std::cerr << record.str() << '\n';
      }
      Message acknowledgement;
      if (!pipe.read(acknowledgement) || acknowledgement.command != ack_command ||
          acknowledgement.request != input.request || acknowledgement.size || !acknowledgement.mapping.empty())
        throw std::runtime_error("Missing output-buffer acknowledgement");
      if (closing)
        break;
    }
    if (session)
      session->close();
    return 0;
  } catch (const std::exception &error) {
    std::cerr << "Cycles worker: " << error.what() << '\n';
    return 1;
  }
}
