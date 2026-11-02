import { createContext, useContext } from 'react';

// 应用级：服务、客户端配置、当前用户、维护状态
export const AppCtx = createContext(null);
export const useApp = () => useContext(AppCtx);

// 项目级：元数据、扩展项、待处理计数、流程、写入范围锁、项目流订阅
export const ProjectCtx = createContext(null);
export const useProject = () => useContext(ProjectCtx);
