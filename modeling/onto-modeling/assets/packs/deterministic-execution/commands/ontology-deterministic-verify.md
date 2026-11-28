验证选中 Operation 的完整确定性执行闭包：Parser 输出协议、显式参数优先合并、输入 Schema、Handler CLI、输出 Schema、重复运行一致性、缺参、边界和错误场景。

`workspace_provider` 使用受控替身验证白名单调用，同时明确真实执行结果取决于运行时工作空间数据。验证不得保存连接信息或凭据。发现 Contract 与 Solver 同时启用时直接失败。
