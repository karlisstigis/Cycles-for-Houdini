// SPDX-License-Identifier: Apache-2.0
#include "staged_output.h"
#include <OpenImageIO/filesystem.h>
#include <stdexcept>
#include <system_error>
#include "../platform/platform.h"

namespace cycles_houdini {
StagedOutput::StagedOutput(const std::string &destination, const std::string &suffix)
    : destination_(std::filesystem::u8path(destination)) {
  if (destination.empty() || destination_.filename().empty())
    throw std::invalid_argument("Output requires a file path");
  for (int attempt = 0; attempt < 16; ++attempt) {
    const auto candidate =
        destination_.parent_path() / OIIO::Filesystem::unique_path(".cycles-%%%%-%%%%-%%%%-%%%%.tmp" + suffix);
    if (platform::reserve_file(candidate)) {
      temporary_ = candidate;
      return;
    }
  }
  throw std::runtime_error("Cannot reserve a unique output file");
}
StagedOutput::~StagedOutput() {
  if (!temporary_.empty()) {
    std::error_code error;
    std::filesystem::remove(temporary_, error);
  }
}
std::string StagedOutput::temporary_filename() const {
  const auto name = temporary_.u8string();
  return {name.begin(), name.end()};
}
bool StagedOutput::commit(bool replace_existing) {
  if (!platform::publish_file(temporary_, destination_, replace_existing))
    return false;
  temporary_.clear();
  return true;
}
} // namespace cycles_houdini
