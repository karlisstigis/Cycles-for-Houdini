// SPDX-License-Identifier: Apache-2.0
#include "tile_display.h"
#include "../core/color_internal.h"
#include <IMG/IMG_TileDevice.h>
#include <IMG/IMG_TileOptions.h>
#include <IMG/IMG_Tags.h>
#include <PXL/PXL_Common.h>
#include <TIL/TIL_TileMPlay.h>
#include <UT/UT_Thread.h>
#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <map>
#include <optional>
#include <thread>
#include <tuple>

namespace cycles_houdini {
namespace {
IMG_ColorModel model(std::size_t channels) {
  if (channels == 1)
    return IMG_1CHAN;
  if (channels == 3)
    return IMG_RGB;
  if (channels == 4)
    return IMG_RGBA;
  throw std::invalid_argument("Unsupported native display channel count");
}
bool is_socket(const std::string &destination) { return destination == "ip" || destination.starts_with("imdisplay:"); }
} // namespace

struct TileDisplay::Impl {
  std::unique_ptr<IMG_TileDevice> device;
  int width, height;
  float pixel_aspect{1};
  std::mutex mutex;
  std::condition_variable ready;
  std::shared_ptr<const FrameEvent> pending;
  std::shared_ptr<const ColorConfiguration> pending_color;
  enum class Error { none, connection, remote_quit, other } error_kind{Error::none};
  std::string error;
  bool active{};
  bool writing{};
  std::uint64_t completed_writes{};
  bool stopping{};
  bool opened{};
  std::string pending_status;
  std::unique_ptr<UT_Thread> writer;
  std::string destination, socket_port;
  std::vector<PassDescriptor> passes;
  std::map<std::string, std::string> options;

  Impl(const std::string &target, const std::string &port, int w, int h, std::vector<PassDescriptor> descriptors,
       float aspect, std::map<std::string, std::string> display_options)
      : width(w), height(h), pixel_aspect(aspect), destination(target), socket_port(port),
        passes(std::move(descriptors)), options(std::move(display_options)) {
    writer.reset(UT_Thread::allocThread(UT_Thread::ThreadSingleRun));
    writer->setThreadName("Cycles native display");
    if (!writer->startThread(
            [](void *data) -> void * {
              static_cast<Impl *>(data)->run();
              return nullptr;
            },
            this))
      throw std::runtime_error("Could not start Houdini display I/O thread");
  }
  void activate() {
    std::lock_guard lock(mutex);
    active = true;
    ready.notify_one();
  }
  void stop() {
    {
      std::lock_guard lock(mutex);
      stopping = true;
      pending.reset();
      pending_color.reset();
    }
    ready.notify_all();
  }
  [[noreturn]] void throw_error() const {
    switch (error_kind) {
    case Error::connection:
      throw DisplayConnectionError(error);
    case Error::remote_quit:
      throw DisplayRemoteQuit(error);
    default:
      throw std::runtime_error(error);
    }
  }
  void open() {
    if (is_socket(destination)) {
      auto mplay = std::make_unique<TIL_TileMPlay>(1, false);
      mplay->setSendPIDFlag(false);
      mplay->terminateOnConnectionLost(false);
      if (!socket_port.empty() || options.contains("reuse_only"))
        mplay->noNewMPlay(true);
      device = std::move(mplay);
    } else
      device.reset(IMG_TileDevice::newDevice(destination.c_str()));
    if (!device)
      throw std::runtime_error("Could not create Houdini image device");
    device->setRenderSourceName(options.contains("rendersource") ? options.at("rendersource").c_str()
                                                                 : "Cycles Houdini");
    IMG_TileOptionList planes;
    for (const auto &pass : passes) {
      auto options = UTmakeUnique<IMG_TileOptions>();
      options->setPlaneInfo(destination.c_str(), pass.id == "combined" ? "C" : pass.id.c_str(), "", IMG_FLOAT,
                            model(pass.channels.size()), pass.color ? IMG_TI_COLOR : IMG_TI_UNDEFINED);
      if (!socket_port.empty()) {
        options->setFormatOption("sockethost", "localhost");
        options->setFormatOption("socketport", socket_port.c_str());
      }
      for (const auto &[key, value] : this->options)
        if (key != "reuse_only")
          options->setFormatOption(key.c_str(), value.c_str());
      planes.append(std::move(options));
    }
    if (!device->openMulti(planes, width, height, width, height, pixel_aspect)) {
      if (auto *mplay = dynamic_cast<TIL_TileMPlay *>(device.get()); mplay && mplay->wasRemoteQuitRequested())
        throw DisplayRemoteQuit("Native display was stopped by viewer");
      if (is_socket(destination))
        throw DisplayConnectionError("Could not open Houdini image device");
      throw std::runtime_error("Could not open Houdini image device");
    }
  }
  void check_connection() {
    if (auto *mplay = dynamic_cast<TIL_TileMPlay *>(device.get())) {
      const bool connected = mplay->checkInterrupt();
      if (mplay->wasRemoteQuitRequested())
        throw DisplayRemoteQuit("Native display was stopped by viewer");
      if (!connected)
        throw DisplayConnectionError("Native display was disconnected");
    }
  }
  void write(const FrameEvent &image, const ColorConfiguration &target) {
    const char *color_tag[] = {PXLgetColorSpaceName(PXL_CS_LINEAR), "1"};
    device->writeCustomTag(IMGtagName(IMG_TAG_COLOR_SPACE).c_str(), 2, color_tag);
    constexpr int rows_per_tile = 32;
    for (int y = 0; y < image.height; y += rows_per_tile) {
      {
        std::lock_guard lock(mutex);
        if (stopping)
          return;
      }
      check_connection();
      const int rows = std::min(rows_per_tile, image.height - y);
      const auto pixels = static_cast<std::size_t>(image.width) * rows;
      std::vector<float> buffer;
      for (const auto &pass : passes) {
        const auto p = std::find_if(image.planes.begin(), image.planes.end(),
                                    [&](const auto &plane) { return plane.pass == pass.id; });
        if (p == image.planes.end())
          buffer.resize(buffer.size() + pixels * pass.channels.size(), 0);
        else {
          const auto start = buffer.size();
          buffer.resize(start + pixels * p->channels);
          copy_image_plane_rows(image, *p, y, rows, buffer.data() + start);
          if (pass.color) {
            if (!image.color_configuration)
              throw std::logic_error("Native frame has no source color configuration");
            convert_working_pixels(*image.color_configuration, target, buffer.data() + start, image.width, rows,
                                   p->channels);
          }
        }
      }
      if (!device->writeTile(buffer.data(), image.x, image.x + image.width - 1, image.y + y, image.y + y + rows - 1)) {
        if (is_socket(destination)) {
          if (auto *mplay = dynamic_cast<TIL_TileMPlay *>(device.get())) {
            mplay->checkInterrupt();
            if (mplay->wasRemoteQuitRequested())
              throw DisplayRemoteQuit("Native display was stopped by viewer");
          }
          throw DisplayConnectionError("Native display connection failed");
        }
        throw std::runtime_error("Native image device write failed");
      }
    }
    device->flush();
  }
  void run() {
    try {
      {
        std::unique_lock lock(mutex);
        ready.wait(lock, [this] { return active || stopping; });
        if (stopping)
          return;
      }
      // Device ownership stays entirely on this HDK thread.
      open();
      {
        std::lock_guard lock(mutex);
        opened = true;
      }
      while (true) {
        std::shared_ptr<const FrameEvent> image;
        std::shared_ptr<const ColorConfiguration> color;
        std::string status;
        {
          std::unique_lock lock(mutex);
          ready.wait_for(lock, std::chrono::milliseconds(40), [this] { return stopping || pending; });
          if (stopping && pending_status.empty())
            break;
          status.swap(pending_status);
          image = std::move(pending);
          color = std::move(pending_color);
          writing = bool(image);
        }
        check_connection();
        if (!status.empty()) {
          auto &mplay = static_cast<TIL_TileMPlay &>(*device);
          mplay.setStatusMsg(status.c_str());
          mplay.flush();
          break;
        }
        if (image)
          write(*image, *color);
        {
          std::lock_guard lock(mutex);
          writing = false;
          if (image)
            ++completed_writes;
        }
        ready.notify_all();
      }
    } catch (const DisplayConnectionError &e) {
      std::lock_guard lock(mutex);
      error_kind = Error::connection;
      error = e.what();
    } catch (const DisplayRemoteQuit &e) {
      std::lock_guard lock(mutex);
      error_kind = Error::remote_quit;
      error = e.what();
    } catch (const std::exception &e) {
      std::lock_guard lock(mutex);
      error_kind = Error::other;
      error = e.what();
    }
    {
      std::lock_guard lock(mutex);
      pending.reset();
      pending_color.reset();
      writing = false;
      opened = false;
    }
    ready.notify_all();
    if (device) {
      device->close(false);
      device.reset();
    }
  }
};

struct TileDisplay::Lane {
  struct Request {
    std::uint64_t owner;
    int width, height;
    float aspect;
    std::vector<PassDescriptor> passes;
    std::shared_ptr<const FrameEvent> image;
    std::shared_ptr<const ColorConfiguration> color;
    std::map<std::string, std::string> options;
  };
  std::mutex mutex;
  std::condition_variable changed;
  std::shared_ptr<Impl> active, retiring;
  std::optional<Request> pending;
  std::thread reaper;
  std::string destination, socket_port;
  std::map<std::string, std::string> options;
  std::uint64_t owner{}, next_owner{}, retired{}, joined{};
  std::uint64_t retiring_owner{};
  bool shutdown{}, exited{}, ever_claimed{};
  bool remote_quit{};

  Lane(std::string target, std::string port) : destination(std::move(target)), socket_port(std::move(port)) {
    reaper = std::thread([this] { run(); });
  }
  ~Lane() {
    // Only an exited lane is removed during normal operation. The process
    // registry otherwise owns it until Python atexit drains it explicitly.
    if (reaper.joinable())
      reaper.join();
  }
  void retire_active() {
    if (active) {
      active->stop();
      retiring = std::move(active);
      retiring_owner = owner;
      ++retired;
    }
  }
  std::uint64_t claim(int width, int height, float aspect, std::vector<PassDescriptor> passes,
                      const std::map<std::string, std::string> &display_options) {
    std::lock_guard lock(mutex);
    if (shutdown || exited)
      throw std::logic_error("Native display destination is shutting down");
    retire_active();
    ever_claimed = true;
    owner = ++next_owner;
    startup_error.clear();
    options = display_options;
    pending = Request{owner, width, height, aspect, std::move(passes), {}, {}, options};
    remote_quit = false;
    changed.notify_all();
    return owner;
  }
  bool is_owner(std::uint64_t token) {
    std::lock_guard lock(mutex);
    return !shutdown && owner == token;
  }
  bool check_error(std::uint64_t token, bool layout_changed) {
    std::lock_guard lock(mutex);
    if (shutdown || owner != token)
      return false;
    if (remote_quit)
      throw DisplayRemoteQuit("Native display was stopped by viewer");
    if (retiring && retiring_owner == token) {
      std::lock_guard writer_lock(retiring->mutex);
      if (retiring->error_kind == Impl::Error::remote_quit)
        retiring->throw_error();
    }
    if (active) {
      std::lock_guard writer_lock(active->mutex);
      if (active->error_kind == Impl::Error::remote_quit)
        active->throw_error();
      if (active->stopping)
        return false;
    }
    if (layout_changed)
      return true;
    if (!startup_error.empty())
      throw std::runtime_error(startup_error);
    if (active) {
      std::lock_guard writer_lock(active->mutex);
      if (active->error_kind != Impl::Error::none)
        active->throw_error();
    }
    return true;
  }
  bool submit(std::uint64_t token, int width, int height, float aspect, const std::vector<PassDescriptor> &passes,
              bool layout_changed, std::shared_ptr<const FrameEvent> image,
              std::shared_ptr<const ColorConfiguration> color) {
    std::lock_guard lock(mutex);
    if (shutdown || owner != token)
      return false;
    if (remote_quit)
      throw DisplayRemoteQuit("Native display was stopped by viewer");
    if (layout_changed)
      startup_error.clear();
    if (layout_changed)
      retire_active();
    if (pending || layout_changed || !active) {
      pending = Request{token, width, height, aspect, passes, std::move(image), std::move(color), options};
      changed.notify_all();
      return true;
    }
    auto &i = *active;
    std::lock_guard writer_lock(i.mutex);
    if (i.error_kind != Impl::Error::none)
      i.throw_error();
    i.pending = std::move(image);
    i.pending_color = std::move(color);
    i.ready.notify_one();
    return true;
  }
  bool set_failure_status(std::uint64_t token, std::string message) {
    std::lock_guard lock(mutex);
    if (shutdown || owner != token || pending || !active)
      return false;
    auto &i = *active;
    {
      std::lock_guard writer_lock(i.mutex);
      if (!i.opened || i.stopping || i.error_kind != Impl::Error::none)
        return false;
      i.pending_status = std::move(message);
      i.pending.reset();
      i.pending_color.reset();
      // Stop pixel writes now. The writer drains this terminal message even
      // if close() hands the connection to the reaper before it wakes up.
      i.stopping = true;
    }
    i.ready.notify_one();
    return true;
  }
  std::uint64_t release(std::uint64_t token) {
    std::lock_guard lock(mutex);
    if (owner != token)
      return joined;
    owner = 0;
    pending.reset();
    retire_active();
    changed.notify_all();
    return retired;
  }
  void wait_joined(std::uint64_t target) {
    std::unique_lock lock(mutex);
    changed.wait(lock, [&] { return joined >= target; });
  }
  std::uint64_t wait_idle(std::uint64_t token) {
    std::unique_lock lock(mutex);
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(10);
    while (true) {
      if (owner != token || shutdown || remote_quit || !startup_error.empty())
        break;
      if (!pending && !retiring && active) {
        std::lock_guard writer_lock(active->mutex);
        if (active->error_kind != Impl::Error::none || (!active->pending && !active->writing))
          break;
      }
      if (changed.wait_until(lock, std::min(deadline, std::chrono::steady_clock::now() +
                                                          std::chrono::milliseconds(10))) == std::cv_status::timeout &&
          std::chrono::steady_clock::now() >= deadline)
        throw std::runtime_error("Timed out waiting for native display writes");
    }
    if (owner != token || shutdown)
      throw std::logic_error("Native display was superseded");
    if (remote_quit)
      throw DisplayRemoteQuit("Native display was stopped by viewer");
    if (!startup_error.empty())
      throw std::runtime_error(startup_error);
    std::lock_guard writer_lock(active->mutex);
    if (active->error_kind != Impl::Error::none)
      active->throw_error();
    return active->completed_writes;
  }
  void request_shutdown() {
    std::lock_guard lock(mutex);
    shutdown = true;
    owner = 0;
    pending.reset();
    retire_active();
    changed.notify_all();
  }
  bool ready(std::uint64_t token) {
    std::lock_guard lock(mutex);
    if (owner != token || shutdown)
      throw std::logic_error("Native display was superseded");
    if (remote_quit)
      throw DisplayRemoteQuit("Native display was stopped by viewer");
    if (!startup_error.empty())
      throw std::runtime_error(startup_error);
    if (pending || retiring || !active)
      return false;
    std::lock_guard writer_lock(active->mutex);
    if (active->error_kind != Impl::Error::none)
      active->throw_error();
    return active->completed_writes > 0 && !active->pending && !active->writing && !active->stopping;
  }
  void run() {
    std::unique_lock lock(mutex);
    while (true) {
      changed.wait(lock, [&] { return retiring || pending || ((shutdown || ever_claimed) && owner == 0 && !active); });
      if (retiring) {
        auto old = retiring;
        const auto old_owner = retiring_owner;
        lock.unlock();
        old->writer.reset(); // The only possibly blocking join is on this reaper.
        lock.lock();
        {
          std::lock_guard writer_lock(old->mutex);
          if (old->error_kind == Impl::Error::remote_quit && owner == old_owner) {
            remote_quit = true;
            pending.reset();
          }
        }
        retiring.reset();
        ++joined;
        changed.notify_all();
        continue;
      }
      if (pending) {
        auto request = std::move(*pending);
        pending.reset();
        lock.unlock();
        std::shared_ptr<Impl> next;
        std::string failure;
        try {
          next = std::make_shared<Impl>(destination, socket_port, request.width, request.height,
                                        std::move(request.passes), request.aspect, std::move(request.options));
        } catch (const std::exception &e) {
          failure = e.what();
        }
        lock.lock();
        if (!failure.empty() && owner == request.owner && !shutdown && !pending) {
          // A failed worker startup cannot be delivered through an Impl.
          // Keep a failed one-frame request for the next eligible call.
          startup_error = std::move(failure);
        }
        if (next && pending && owner == request.owner && pending->owner == request.owner &&
            pending->width == request.width && pending->height == request.height && pending->aspect == request.aspect &&
            pending->passes == next->passes) {
          // A newer frame for the same unopened layout replaces the old one.
          request = std::move(*pending);
          pending.reset();
        }
        if (next && owner == request.owner && !shutdown && !retiring && !pending) {
          active = std::move(next);
          {
            std::lock_guard writer_lock(active->mutex);
            active->pending = std::move(request.image);
            active->pending_color = std::move(request.color);
          }
          active->activate();
          changed.notify_all();
        } else if (next) {
          next->stop();
          retiring = std::move(next);
          retiring_owner = request.owner;
          ++retired;
        }
        continue;
      }
      if ((shutdown || ever_claimed) && owner == 0 && !active) {
        exited = true;
        changed.notify_all();
        return;
      }
    }
  }
  std::string startup_error;
};

struct TileDisplay::Registry {
  std::mutex mutex;
  std::map<std::tuple<std::string, std::string, std::map<std::string, std::string>>, std::shared_ptr<Lane>> lanes;
  bool shutting_down{};
};
TileDisplay::Registry &TileDisplay::registry() {
  // Never destroy threads from a Windows DLL static destructor. Python atexit
  // calls shutdown_all before extension unload, outside the loader lock.
  static auto *value = new Registry;
  return *value;
}
std::pair<std::shared_ptr<TileDisplay::Lane>, std::uint64_t>
TileDisplay::claim_lane(const std::string &destination, const std::string &socket_port, int width, int height,
                        float aspect, const std::vector<PassDescriptor> &passes,
                        const std::map<std::string, std::string> &options) {
  auto &r = registry();
  std::lock_guard lock(r.mutex);
  if (r.shutting_down)
    throw std::logic_error("Native display destinations are shutting down");
  for (auto it = r.lanes.begin(); it != r.lanes.end();) {
    auto lane = it->second;
    bool exited;
    {
      std::lock_guard lane_lock(lane->mutex);
      exited = lane->exited;
    }
    if (exited)
      it = r.lanes.erase(it); // The exited thread is safe to join here.
    else
      ++it;
  }
  // One MPlay window has one retirement lane, even when frame/source metadata
  // changes. The reaper closes the old connection before opening its successor.
  const auto identity =
      options.contains("label") ? std::map<std::string, std::string>{{"label", options.at("label")}} : options;
  const auto key = std::make_tuple(destination, socket_port, identity);
  auto &lane = r.lanes[key];
  if (!lane)
    lane = std::make_shared<Lane>(destination, socket_port);
  std::uint64_t token;
  try {
    token = lane->claim(width, height, aspect, passes, options);
  } catch (const std::logic_error &) {
    bool exited;
    {
      std::lock_guard lane_lock(lane->mutex);
      exited = lane->exited;
    }
    if (!exited)
      throw;
    lane = std::make_shared<Lane>(destination, socket_port);
    token = lane->claim(width, height, aspect, passes, options);
  }
  return {lane, token};
}
void TileDisplay::shutdown_all() {
  auto &r = registry();
  std::vector<std::shared_ptr<Lane>> lanes;
  {
    std::lock_guard lock(r.mutex);
    r.shutting_down = true;
    for (const auto &[key, lane] : r.lanes)
      lanes.push_back(lane);
  }
  for (auto &lane : lanes)
    lane->request_shutdown();
  for (auto &lane : lanes)
    if (lane->reaper.joinable())
      lane->reaper.join();
  {
    std::lock_guard lock(r.mutex);
    r.lanes.clear();
  }
}

TileDisplay::TileDisplay(const std::string &destination, const std::string &socket_port, int width, int height,
                         std::vector<PassDescriptor> passes, std::map<std::string, std::string> options)
    : destination_(destination), socket_port_(socket_port), width_(width), height_(height), passes_(std::move(passes)),
      options_(std::move(options)) {}
TileDisplay::~TileDisplay() { close(); }
void TileDisplay::activate() {
  std::lock_guard operation_lock(operation_mutex_);
  if (closed_)
    throw std::logic_error("Houdini image device is closed");
  if (lane_)
    return;
  auto [lane, token] = claim_lane(destination_, socket_port_, width_, height_, pixel_aspect_, passes_, options_);
  owner_ = token;
  lane_ = std::move(lane);
}
void TileDisplay::present(std::shared_ptr<const FrameEvent> image, std::uint64_t presentation_sequence) {
  if (!image)
    throw std::invalid_argument("No native frame is available for presentation");
  validate_frame_image(*image);
  std::lock_guard operation_lock(operation_mutex_);
  if (closed_ || !lane_)
    throw std::logic_error("Houdini image device is closed");
  if (!lane_->is_owner(owner_))
    return; // A pinned presenter from the replaced session cannot cancel its successor.
  const auto serial = presentation_sequence ? presentation_sequence : image->sequence;
  const bool layout_changed =
      !image->requested_passes.empty() && (image->requested_passes != passes_ || image->pixel_aspect != pixel_aspect_);
  if (has_sequence_ && (serial < sequence_ || (serial == sequence_ && layout_changed)))
    return;
  if (!lane_->check_error(owner_, layout_changed))
    return;
  const auto target = presentation_color_configuration();
  std::string color_id = target->policy().render_policy_id;
  if (has_sequence_ && serial == sequence_ && color_id == presented_color_)
    return;
  if (image->width < 1 || image->height < 1 || image->x < 0 || image->y < 0 || image->x + image->width > width_ ||
      image->y + image->height > height_)
    throw std::invalid_argument("Image dimensions do not match native display");
  const auto new_aspect = layout_changed ? image->pixel_aspect : pixel_aspect_;
  const auto new_passes = layout_changed ? image->requested_passes : passes_;
  if (image->color_configuration && !can_convert_working_pixels(*image->color_configuration, *target) &&
      std::any_of(new_passes.begin(), new_passes.end(), [](const auto &pass) { return pass.color; }))
    throw PreviousColorConfiguration();
  if (!lane_->submit(owner_, width_, height_, new_aspect, new_passes, layout_changed, std::move(image), target))
    return;
  if (layout_changed) {
    passes_ = new_passes;
    pixel_aspect_ = new_aspect;
  }
  presented_color_.swap(color_id);
  sequence_ = serial;
  has_sequence_ = true;
}
void TileDisplay::close() {
  std::lock_guard operation_lock(operation_mutex_);
  close_impl();
}
std::uint64_t TileDisplay::wait_for_idle() {
  std::lock_guard operation_lock(operation_mutex_);
  if (closed_ || !lane_)
    throw std::logic_error("Houdini image device is closed");
  return lane_->wait_idle(owner_);
}
void TileDisplay::close_impl() {
  if (closed_)
    return;
  closed_ = true;
  if (lane_) {
    const auto target = lane_->release(owner_);
    if (!is_socket(destination_))
      lane_->wait_joined(target); // Keep file/EXR close finalization semantics.
    lane_.reset();
  }
}
bool TileDisplay::ready() {
  std::lock_guard operation_lock(operation_mutex_);
  if (closed_ || !lane_)
    throw std::logic_error("Houdini image device is closed");
  return lane_->ready(owner_);
}
bool TileDisplay::set_failure_status(std::string message) {
  std::lock_guard operation_lock(operation_mutex_);
  if (closed_ || !lane_ || !is_socket(destination_) || message.empty() || message.find('\0') != std::string::npos)
    return false;
  return lane_->set_failure_status(owner_, std::move(message));
}
} // namespace cycles_houdini
