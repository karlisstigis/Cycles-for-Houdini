// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "cycles_houdini/session.h"
#include "scene/pass.h"
namespace ccl {
class Scene;
}
namespace cycles_houdini {
// Engine-facing helpers; public value contracts remain in session.h.
ccl::PassType pass_type(const std::string &id);
std::string json_string(const std::string &value);
std::string crypto_hash(const std::string &name);
std::shared_ptr<const std::map<std::string, std::string>> crypto_scene_metadata(ccl::Scene *scene,
                                                                                const CryptomatteSettings &settings);
} // namespace cycles_houdini
