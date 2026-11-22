// Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import * as THREE from 'three';
import { resolveGraphTheme } from './graph-theme.js';

/** Neutral studio lighting follows view rotation, independently of camera distance and pan. */
export function createGraphLighting(mode = 'light') {
  const rig = new THREE.Group();
  const lights = { ambient: new THREE.AmbientLight(0xffffff) };
  rig.add(lights.ambient);
  for (const [role, position] of Object.entries({
    key: [-500, 650, 900], fill: [700, -120, 300], rim: [280, 280, -800],
  })) {
    const light = new THREE.DirectionalLight(0xffffff);
    light.position.set(...position);
    // Both light and target rotate together; a default world-space target would drift on orbit.
    rig.add(light, light.target);
    lights[role] = light;
  }
  const setTheme = (nextMode) => {
    const theme = resolveGraphTheme(nextMode);
    Object.entries(lights).forEach(([role, light]) => { light.intensity = theme.lights[role]; });
  };
  const followCamera = (camera) => { rig.quaternion.copy(camera.quaternion); };
  setTheme(mode);
  return { rig, lights, setTheme, followCamera };
}
