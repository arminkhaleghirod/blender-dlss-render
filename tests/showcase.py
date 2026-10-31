"""Showcase scene for the README benchmarks: glass, metal, a subdivided monkey, depth of field, a slow dolly."""
import math

import bpy


def mat(name, **inputs):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    p = m.node_tree.nodes["Principled BSDF"]
    for k, v in inputs.items():
        p.inputs[k].default_value = v
    return m


def build():
    sc = bpy.context.scene
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    for c in list(sc.timeline_markers):
        sc.timeline_markers.remove(c)

    bpy.ops.mesh.primitive_plane_add(size=40, location=(0, 0, 0))
    floor = bpy.context.object
    fm = mat("floor", **{"Base Color": (0.55, 0.52, 0.5, 1), "Roughness": 0.35})
    nt = fm.node_tree
    chk = nt.nodes.new("ShaderNodeTexChecker")
    chk.inputs["Scale"].default_value = 18
    chk.inputs["Color1"].default_value = (0.6, 0.58, 0.55, 1)
    chk.inputs["Color2"].default_value = (0.25, 0.24, 0.23, 1)
    nt.links.new(chk.outputs["Color"], nt.nodes["Principled BSDF"].inputs["Base Color"])
    floor.data.materials.append(fm)

    bpy.ops.mesh.primitive_monkey_add(location=(0, 0, 1.0), rotation=(0, 0, math.radians(-20)))
    monkey = bpy.context.object
    monkey.modifiers.new("s", "SUBSURF").levels = 2
    monkey.modifiers["s"].render_levels = 2
    bpy.ops.object.shade_smooth()
    monkey.data.materials.append(mat("orange", **{"Base Color": (0.9, 0.3, 0.05, 1), "Roughness": 0.3,
                                                  "Subsurface Weight": 0.3}))

    bpy.ops.mesh.primitive_uv_sphere_add(location=(2.3, 0.6, 0.9), radius=0.9, segments=96, ring_count=48)
    bpy.ops.object.shade_smooth()
    bpy.context.object.name = "Glass"
    bpy.context.object.data.materials.append(mat("glass", **{"Transmission Weight": 1.0, "Roughness": 0.0,
                                                             "IOR": 1.45}))

    bpy.ops.mesh.primitive_torus_add(location=(-2.4, 0.4, 0.55), major_radius=0.8, minor_radius=0.3,
                                     rotation=(math.radians(60), 0, 0))
    bpy.ops.object.shade_smooth()
    torus = bpy.context.object
    torus.data.materials.append(mat("gold", **{"Base Color": (1.0, 0.75, 0.35, 1), "Metallic": 1.0,
                                               "Roughness": 0.18}))
    torus.keyframe_insert("rotation_euler", frame=1)
    torus.rotation_euler[2] = math.radians(90)
    torus.keyframe_insert("rotation_euler", frame=48)

    bpy.ops.mesh.primitive_cube_add(location=(0.8, 3.5, 1.5), scale=(3, 0.1, 1.5))
    bpy.context.object.data.materials.append(mat("wall", **{"Base Color": (0.1, 0.12, 0.16, 1)}))

    bpy.ops.object.light_add(type="AREA", location=(3, -3, 5))
    key = bpy.context.object
    key.data.energy, key.data.size = 900, 3
    key.rotation_euler = (math.radians(35), math.radians(25), math.radians(40))
    bpy.ops.object.light_add(type="AREA", location=(-4, 1, 2.5))
    rim = bpy.context.object
    rim.data.energy, rim.data.size, rim.data.color = 300, 1, (0.4, 0.6, 1.0)
    rim.rotation_euler = (0, math.radians(-70), 0)

    world = sc.world or bpy.data.worlds.new("w")
    sc.world = world
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.03, 0.035, 0.05, 1)
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = 1.0

    bpy.ops.object.camera_add(location=(0.4, -7.5, 2.4), rotation=(math.radians(78), 0, math.radians(2)))
    cam = bpy.context.object
    cam.name = "Cam"
    cam.data.lens = 40
    cam.data.dof.use_dof = True
    cam.data.dof.focus_object = monkey
    cam.data.dof.aperture_fstop = 2.2
    cam.keyframe_insert("location", frame=1)
    cam.location = (-0.6, -6.8, 2.2)
    cam.keyframe_insert("location", frame=48)
    sc.camera = cam

    bpy.ops.object.camera_add(location=(3.6, -2.2, 1.3), rotation=(math.radians(84), 0, math.radians(30)))
    bpy.context.object.name = "CamGlass"
    bpy.context.object.data.lens = 70

    sc.render.engine = "CYCLES"
    prefs = bpy.context.preferences.addons["cycles"].preferences
    prefs.compute_device_type = "OPTIX"
    prefs.refresh_devices()
    for d in prefs.devices:
        d.use = d.type == "OPTIX"
    sc.cycles.device = "GPU"
    sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage = 1920, 1080, 100
    sc.frame_start, sc.frame_end, sc.frame_step = 1, 48, 1
    sc.render.fps = 24
    sc.render.image_settings.media_type = "IMAGE"
    sc.render.image_settings.file_format = "PNG"
    sc.view_settings.view_transform = "AgX"
    return sc
