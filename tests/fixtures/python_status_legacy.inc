// SPDX-License-Identifier: Apache-2.0
// Pre-extraction conversion oracle (2026-11-09), extended for mesh refit and subdivision topology cache telemetry.
void denoise_metadata(PyObject* target,const std::string& backend,const std::string& device) {
    Ref name{PyUnicode_FromString(backend.c_str())},identity{PyUnicode_FromString(device.c_str())};
    if(!name.object || !identity.object || PyDict_SetItemString(target,"denoiser",name)<0 || PyDict_SetItemString(target,"denoise_device_id",identity)<0)
        throw std::runtime_error("Could not create denoising metadata");
}
bool motion_warning_metadata(PyObject* result,const ch::PythonStatusExtraction& stats){
    for(const auto& [name,value]:{std::pair{"material_motion_cooks",stats.material_motion_cooks},
        std::pair{"material_motion_samples",stats.material_motion_samples},std::pair{"material_motion_cache_hits",stats.material_motion_cache_hits}}){
        Ref number{PyLong_FromUnsignedLongLong(value)};if(!number.object || PyDict_SetItemString(result,name,number)<0)return false;
    }
    Ref warnings{PyList_New(0)};if(!warnings.object)return false;
    for(const auto& warning:stats.motion_warnings){Ref value{PyUnicode_FromString(warning.c_str())};if(!value.object || PyList_Append(warnings,value)<0)return false;}
    return PyDict_SetItemString(result,"motion_warnings",warnings)==0;
}
bool source_diagnostic_metadata(PyObject* result,const std::vector<ch::SourceDiagnostic>& issues){
    Ref values{PyList_New(0)};if(!values.object)return false;
    for(const auto& d:issues){
        Ref value{Py_BuildValue("{s:s,s:s,s:s,s:s,s:s,s:s,s:s}","code",d.code.c_str(),"source",d.source.c_str(),
            "resource",d.resource.c_str(),"message",d.message.c_str(),"action",d.action.c_str(),"severity",d.severity.c_str(),"attribute",d.attribute.c_str())};
        if(!value.object || PyList_Append(values,value)<0)return false;
    }
    return PyDict_SetItemString(result,"source_diagnostics",values)==0;
}
bool volume_vector_metadata(PyObject* result,const ch::SessionStatus& status){
    for(const auto& [name,value]:{std::pair{"volume_vector_merges",status.volume_vector_merges},
        std::pair{"volume_vector_fields",std::uint64_t(status.volume_vector_fields)},
        std::pair{"volume_vector_source_bytes",std::uint64_t(status.volume_vector_source_bytes)}}){
        Ref number{PyLong_FromUnsignedLongLong(value)};
        if(!number.object || PyDict_SetItemString(result,name,number)<0)return false;
    }
    return true;
}
bool subdivision_topology_cache_metadata(PyObject* result,const ch::SessionStatus& status){
    for(const auto& [name,value]:{
        std::pair{"subdivision_topology_cache_hits",status.subdivision_topology_cache_hits},
        std::pair{"subdivision_topology_cache_misses",status.subdivision_topology_cache_misses},
        std::pair{"subdivision_topology_cache_admissions",status.subdivision_topology_cache_admissions},
        std::pair{"subdivision_topology_cache_evictions",status.subdivision_topology_cache_evictions},
        std::pair{"subdivision_topology_cache_oversized",status.subdivision_topology_cache_oversized},
        std::pair{"subdivision_topology_cache_entries",std::uint64_t(status.subdivision_topology_cache_entries)},
        std::pair{"subdivision_topology_cache_bytes",std::uint64_t(status.subdivision_topology_cache_bytes)},
        std::pair{"subdivision_topology_cache_budget_bytes",std::uint64_t(status.subdivision_topology_cache_budget_bytes)},
        std::pair{"subdivision_topology_cache_active_retired_bytes",std::uint64_t(status.subdivision_topology_cache_active_retired_bytes)}}){
        Ref number{PyLong_FromUnsignedLongLong(value)};
        if(!number.object || PyDict_SetItemString(result,name,number)<0)return false;
    }
    return true;
}
PyObject* status_value(const ch::SessionStatus& s,const ch::PythonStatusExtraction& a={}){Ref result{Py_BuildValue("{s:s,s:K,s:s,s:s,s:d,s:d,s:d,s:K,s:K,s:K,s:K,s:K,s:d,s:d,s:K,s:K,s:K,s:K,s:K,s:K,s:K,s:K,s:K,s:K,s:K,s:O,s:K,s:K,s:K,s:K,s:K,s:K,s:K,s:K}","state",ch::state_name(s.state),"generation",s.generation,"message",s.message.c_str(),"error",s.error.c_str(),"progress",s.progress,"elapsed_seconds",s.elapsed_seconds,"first_image_seconds",s.first_image_seconds,"geometry_conversions",s.geometry_conversions,"object_updates",s.object_updates,"owned_geometry_bytes",s.owned_geometry_bytes,"device_allocation_bytes",s.device_allocation_bytes,"device_allocation_peak_bytes",s.device_allocation_peak_bytes,"cooking_seconds",a.cooking_seconds,"extraction_seconds",a.conversion_seconds,"extractions",a.extractions,"cache_hits",a.cache_hits,"geometry_resources",s.geometry_resources,"object_resources",s.object_resources,"material_resources",s.material_resources,"light_resources",s.light_resources,"packed_instances",a.packed_instances,"index_instance_ids",a.index_instance_ids,"engine_shader_resources",s.engine_shader_resources,"retired_shader_resources",s.retired_shader_resources,"shader_reuses",s.shader_reuses,"volume_ray_marching",s.volume_ray_marching?Py_True:Py_False,"mesh_variant_resources",s.mesh_variant_resources,"mesh_variant_bytes",s.mesh_variant_bytes,"retained_mesh_source_bytes",s.retained_mesh_source_bytes,"displacement_rebuilds",s.displacement_rebuilds,"subdivision_control_faces",s.subdivision_control_faces,"subdivision_vertices",s.subdivision_vertices,"subdivision_triangles",s.subdivision_triangles,"subdivision_tessellations",s.subdivision_tessellations)};if(!result.object)return nullptr;
    for(const auto& [name,value]:{std::pair{"image_iris_preparations",s.image_iris_preparations},std::pair{"image_iris_table_bytes",s.image_iris_table_bytes}}){
        Ref field{PyLong_FromUnsignedLongLong(value)};if(!field.object || PyDict_SetItemString(result,name,field)<0)return nullptr;
    }
    {Ref field{PyFloat_FromDouble(s.image_iris_preparation_seconds)};if(!field.object || PyDict_SetItemString(result,"image_iris_preparation_seconds",field)<0)return nullptr;}
    for(const auto& [name,value]:{std::pair{"mesh_family_resources",s.mesh_family_resources},std::pair{"mesh_family_members",s.mesh_family_members},std::pair{"mesh_variant_bindings",s.mesh_variant_bindings},std::pair{"mesh_metric_records",s.mesh_metric_records},std::pair{"mesh_refits",s.mesh_refits}}){
        Ref field{PyLong_FromSize_t(value)};if(!field.object || PyDict_SetItemString(result,name,field)<0)return nullptr;
    }
    {Ref objects{PyLong_FromUnsignedLongLong(s.dielectric_priority_objects)},low{PyLong_FromLong(s.dielectric_priority_min)},high{PyLong_FromLong(s.dielectric_priority_max)};
     if(!objects.object || !low.object || !high.object || PyDict_SetItemString(result,"engine_nested_dielectrics",s.engine_nested_dielectrics?Py_True:Py_False)<0 ||
        PyDict_SetItemString(result,"dielectric_priority_objects",objects)<0 || PyDict_SetItemString(result,"dielectric_priority_min",low)<0 ||
        PyDict_SetItemString(result,"dielectric_priority_max",high)<0)return nullptr;}
    {Ref unresolved{PyLong_FromUnsignedLongLong(s.nested_probe_unresolved)},depth{PyLong_FromLong(s.nested_stack_depth)};
     if(!unresolved.object || !depth.object || PyDict_SetItemString(result,"nested_probe_unresolved",unresolved)<0 ||
        PyDict_SetItemString(result,"nested_stack_depth",depth)<0 ||
        PyDict_SetItemString(result,"nested_stack_depth_clamped",s.nested_stack_depth_clamped?Py_True:Py_False)<0)return nullptr;}
    Ref epoch{PyLong_FromUnsignedLongLong(s.color_epoch)},target_epoch{PyLong_FromUnsignedLongLong(s.color_target_epoch)};if(!epoch.object || !target_epoch.object || PyDict_SetItemString(result,"color_epoch",epoch)<0 || PyDict_SetItemString(result,"color_target_epoch",target_epoch)<0 || PyDict_SetItemString(result,"color_update_pending",s.color_update_pending?Py_True:Py_False)<0)return nullptr;denoise_metadata(result,s.denoiser,s.denoise_device_id);Ref count{PyLong_FromLong(s.current_sample)};if(!count.object || PyDict_SetItemString(result,"current_sample",count)<0)return nullptr;Ref reuses{PyLong_FromUnsignedLongLong(s.unchanged_reuses)};if(!reuses.object || PyDict_SetItemString(result,"unchanged_reuses",reuses)<0)return nullptr;Ref direct{PyLong_FromUnsignedLongLong(s.direct_attribute_updates)},expanded{PyLong_FromUnsignedLongLong(s.expanded_attribute_updates)},fallbacks{PyLong_FromUnsignedLongLong(s.attribute_fallback_count)};if(!direct.object || !expanded.object || !fallbacks.object || PyDict_SetItemString(result,"direct_attribute_updates",direct)<0 || PyDict_SetItemString(result,"expanded_attribute_updates",expanded)<0 || PyDict_SetItemString(result,"attribute_fallback_count",fallbacks)<0)return nullptr;Ref reason{PyUnicode_FromString(s.attribute_fallback_reason.c_str())};if(!reason.object || PyDict_SetItemString(result,"attribute_fallback_reason",reason)<0)return nullptr;Ref devices{PyList_New(0)};for(const auto& id:s.render_device_ids){Ref value{PyUnicode_FromString(id.c_str())};if(!value.object || PyList_Append(devices,value)<0)return nullptr;}if(PyDict_SetItemString(result,"render_device_ids",devices)<0 || !motion_warning_metadata(result,a) || !source_diagnostic_metadata(result,s.source_diagnostics) || !volume_vector_metadata(result,s) || !subdivision_topology_cache_metadata(result,s))return nullptr;return Py_NewRef(result);}
PyObject* capabilities(PyObject*,PyObject*) {try{
    const auto caps=ch::capabilities();
    Ref result{Py_BuildValue("{s:s,s:O,s:O,s:O,s:O,s:O,s:O,s:O,s:O}","engine_commit",caps.engine_commit.c_str(),"meshes",caps.meshes?Py_True:Py_False,"point_spheres",caps.point_spheres?Py_True:Py_False,"gaussian_splats",caps.gaussian_splats?Py_True:Py_False,"linear_round_curves",caps.linear_round_curves?Py_True:Py_False,"volumes",caps.volumes?Py_True:Py_False,"native_ipr",Py_True,"osl",caps.osl?Py_True:Py_False,"development_build",Py_True)};
    if(!result.object)return nullptr;
    if(PyDict_SetItemString(result,"image_iris",caps.image_iris?Py_True:Py_False)<0)return nullptr;
    if(PyDict_SetItemString(result,"native_scene_definitions",Py_True)<0)return nullptr;
    if(PyDict_SetItemString(result,"bdpt_prototype",caps.bdpt_prototype?Py_True:Py_False)<0)return nullptr;
    if(PyDict_SetItemString(result,"path_guiding",caps.path_guiding?Py_True:Py_False)<0)return nullptr;
    Ref graph_definitions{PyList_New(0)};if(!graph_definitions.object)return nullptr;
    for(const auto& id:ch::surface_graph_definitions()){Ref value{PyUnicode_FromString(id.c_str())};if(!value.object || PyList_Append(graph_definitions,value)<0)return nullptr;}
    if(PyDict_SetItemString(result,"surface_graph_definitions",graph_definitions)<0)return nullptr;
    Ref volume_definitions{PyList_New(0)};if(!volume_definitions.object)return nullptr;
    for(const auto& id:ch::volume_graph_definitions()){Ref value{PyUnicode_FromString(id.c_str())};if(!value.object || PyList_Append(volume_definitions,value)<0)return nullptr;}
    if(PyDict_SetItemString(result,"volume_graph_definitions",volume_definitions)<0)return nullptr;
    Ref world_definitions{PyList_New(0)};if(!world_definitions.object)return nullptr;
    for(const auto& id:ch::world_graph_definitions()){Ref value{PyUnicode_FromString(id.c_str())};if(!value.object || PyList_Append(world_definitions,value)<0)return nullptr;}
    if(PyDict_SetItemString(result,"world_graph_definitions",world_definitions)<0 || PyDict_SetItemString(result,"native_world_materials",Py_True)<0)return nullptr;
    Ref light_definitions{PyList_New(0)};if(!light_definitions.object)return nullptr;
    for(const auto& id:ch::light_graph_definitions()){Ref value{PyUnicode_FromString(id.c_str())};if(!value.object || PyList_Append(light_definitions,value)<0)return nullptr;}
    if(PyDict_SetItemString(result,"light_graph_definitions",light_definitions)<0)return nullptr;
    if(PyDict_SetItemString(result,"native_material_vops",Py_True)<0)return nullptr;
    for(const auto* name:{"native_transparent_film","native_transparent_glass","native_surface_holdout"})
        if(PyDict_SetItemString(result,name,Py_True)<0)return nullptr;
    for(const auto* name:{"native_linear_subdivision","native_catmull_clark_subdivision","native_subdivision_creases","native_subdivision_polygon_soups"})
        if(PyDict_SetItemString(result,name,Py_True)<0)return nullptr;
    if(PyDict_SetItemString(result,"material_subnetworks",Py_True)<0)return nullptr;
    for(const auto* name:{"native_object_materials","native_primitive_materials","native_packed_materials","native_point_materials","native_curve_materials"})if(PyDict_SetItemString(result,name,Py_True)<0)return nullptr;
    if(PyDict_SetItemString(result,"native_material_overrides",Py_True)<0)return nullptr;
    if(PyDict_SetItemString(result,"native_material_stylesheets",Py_True)<0)return nullptr;
    Ref style_categories{Py_BuildValue("[ssss]","material","materialParameters","surfaceParameters","displaceParameters")};
    if(!style_categories.object || PyDict_SetItemString(result,"native_stylesheet_categories",style_categories)<0)return nullptr;
    Ref ramp_bases{Py_BuildValue("[sssssss]","constant","linear","catmull_rom","monotone_cubic","bezier","bspline","hermite")};
    Ref ramp_max_keys{PyLong_FromLong(32)};
    if(!ramp_bases.object || !ramp_max_keys.object || PyDict_SetItemString(result,"material_ramp_bases",ramp_bases)<0 ||
       PyDict_SetItemString(result,"material_ramp_max_keys",ramp_max_keys)<0)return nullptr;
    if(PyDict_SetItemString(result,"graph_volume_output",Py_True)<0)return nullptr;
    if(PyDict_SetItemString(result,"graph_displacement_output",Py_True)<0)return nullptr;
    for(const auto* name:{"graph_combined_outputs","closed_mesh_volumes"})if(PyDict_SetItemString(result,name,Py_True)<0)return nullptr;
    for(const auto* name:{"native_transform_motion","native_velocity_motion","native_deformation_motion","native_volume_velocity_motion","native_packed_deformation","native_packed_velocity"})if(PyDict_SetItemString(result,name,Py_True)<0)return nullptr;
    for(const auto* name:{"native_camera_source_recovery","native_animated_camera_intrinsics","native_perspective_zoom_motion","native_panoramic_cameras","native_panoramic_motion_vectors","native_panoramic_depth_of_field"})if(PyDict_SetItemString(result,name,Py_True)<0)return nullptr;
    for(const auto* name:{"particle_lifetime_clipping","particle_lifetime_motion","native_moving_lights","native_velocity_authored_mesh_normals"})if(PyDict_SetItemString(result,name,Py_False)<0)return nullptr;
    for(const auto* name:{"native_obj_scene_selection","native_obj_ray_visibility","native_numeric_lights","native_denoising","native_denoising_guides","native_denoising_comparison"})if(PyDict_SetItemString(result,name,Py_True)<0)return nullptr;
    Ref light_types{Py_BuildValue("[sssssss]","point","grid","disk","sphere","distant","sun","envlight")};if(!light_types.object || PyDict_SetItemString(result,"native_houdini_light_types",light_types)<0)return nullptr;
    Ref volume_integrator{PyUnicode_FromString("ray_marching")};if(!volume_integrator || PyDict_SetItemString(result,"volume_motion_integrator",volume_integrator)<0)return nullptr;
    return Py_NewRef(result);
}catch(const std::exception& e){return failure(e);}}
