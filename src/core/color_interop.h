// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <OpenColorIO/OpenColorIO.h>
#include <map>
#include <string>

namespace cycles_houdini {
struct ColorInteropIdentity {
  std::string id, selection;
};
struct PortableInputSpace {
  std::string colorspace, label, error;
};
struct PortableColorConfiguration {
  OCIO_NAMESPACE::ConstConfigRcPtr config;
  std::map<std::string, PortableInputSpace> inputs;
  std::string builtin_configuration;
};
// Compatibility implementation of the CIF namespace/local-ID rules for the
// installed OCIO 2.5 SDK, which predates findColorSpaceForID and local-ID helpers.
std::string sanitize_interop_component(const std::string &);
bool valid_interop_id(const std::string &);
OCIO_NAMESPACE::ConstColorSpaceRcPtr find_interop_space(const OCIO_NAMESPACE::ConstConfigRcPtr &, const std::string &);
ColorInteropIdentity working_interop_identity(const OCIO_NAMESPACE::ConstConfigRcPtr &);
PortableColorConfiguration make_portable_color_configuration(const OCIO_NAMESPACE::ConstConfigRcPtr &);
PortableInputSpace portable_input_space(const OCIO_NAMESPACE::ConstConfigRcPtr &user,
                                        const PortableColorConfiguration &, const std::string &id);
} // namespace cycles_houdini
