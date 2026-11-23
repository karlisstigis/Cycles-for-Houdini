// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <stdexcept>
#include <memory>
#include <string>
namespace cycles_houdini {
class IesSourceError : public std::invalid_argument {
public:
  using std::invalid_argument::invalid_argument;
};
struct IesAsset {
  std::string filename, revision, content_revision;
};
// Validate bounded owned text before entering Cycles' permissive IES parser.
// Only the copied text reaches shader compilation; it never reopens the file.
std::shared_ptr<const std::string> prepare_ies_asset(const std::string &filename, const std::string &text,
                                                     IesAsset &asset);
bool ies_asset_changed(const IesAsset &asset);
} // namespace cycles_houdini
