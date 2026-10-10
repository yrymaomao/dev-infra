# 共享 Runtime＋多逻辑 Agent OpenClaw：CRM 只读 Dev 接入执行计划

日期：2026-10-10。状态：执行中，尚未发布或验收完成。
作者：rongyu.yuan。依据：用户批准的共享 Runtime、多逻辑 Agent 详细设计及执行计划。

## 1. 目标与已确认决策

真实链路：emf-ai CRM 页面 → 共享 BFF → 同一 OpenClaw 宿主的 crm 逻辑 Agent → 共享 Runtime → CRM MCP → CRM Java → Evidence → 模型 → 建议回复。

成功标准：真实建议或明确阻塞可追溯；CRM Agent 业务写入为零；Supply Chain 回归通过；精确版本可回退。业务 BLOCKED 不能冒充模型建议成功。

- 各仓库沿用 codex/crm-supply-chain-integration-20260919；Adapter 创建同名分支，不新建 Runtime 分支或工作树。
- 一个共享 Runtime、一个 composition、一个共享 OpenClaw 宿主；逻辑 Agent 为 main、crm。
- 允许短暂维护窗口，实际切换时间发布前另行确认。
- 复用 emf-ai CRM 页面；不在 emf-crm 重建聊天入口。
- 仅 READ/PREVIEW；不实施 Gateway wire v2、审批或业务 WRITE。
- 已有独立 CRM 空数据库只保留备用，本轮不接入，不重复初始化、不删除。

## 2. 工作包与门槛

### P0：保护 WIP，冻结发布与回退基线

- [ ] 核对分支、完整 SHA、工作树、差异和归属；保存 staged/unstaged 二进制补丁及文件指纹，复制明确在范围内的未跟踪源文件。不得暂存、提交或发布他人 WIP。
- [ ] Runtime 安全交接前不切换分支或编辑重叠文件；不创建额外工作树绕开限制。
- [ ] 从 Dev 运行容器、安装包、Jenkins Build Data 与受保护配置的脱敏投影核定 Runtime/BFF/OpenClaw/Adapter/Base AI/Agents/前端实际版本。
- [ ] 核定 composition、generation、reception、Workflow、模型、MCP endpoint/service_type、数据库迁移头、状态卷、账本及网络命名空间和 loopback 服务归属。
- [ ] 保留精确原镜像、包、配置修订及恢复命令，防止被构建清理策略删除。
- [ ] Adapter 无远端时交付 Git bundle、提交及包摘要，不擅自创建云端仓库。

门槛：现在运行什么、候选改变什么、失败恢复什么均有证据。缺少证据不进入真实发布。

### P1：Runtime 兼容性核验，优先复用

- [ ] 在实际安装包或精确副本验证 MCP 先初始化，ERP/CRM 使用同一受治理 ToolInvoker，MODEL 不注入；allowlist、凭证隔离和失败清理有效。
- [ ] 已支持则不改 Runtime，只记录兼容版本；未支持只纳入已有最小启动修复，不带入 WRITE、配置清理或并发任务改动。
- [ ] 没有干净且可审查候选时阻断启用，不整体发布脏整合分支。
- [ ] 核定 Base AI/CRM Adapter/CRM Agent 的版本、RECORD 摘要及共享依赖回归。

门槛：最终安装包真实启动四 Provider，Runtime 保持业务无关，不凭源码判断已部署。

### P2：通用多逻辑 Agent Adapter

采用失败测试 → 最小实现 → 回归 → 独立提交。

提交 A：HostAdapterManager 与生命周期。

- [ ] 替换单 Adapter singleton；每逻辑 Agent 独立 PersistentAdapter、TrustedRunBroker、运行上下文、generation、reception、账本、workspace、agentDir。
- [ ] main 路径保持不变，crm 新增独立目录；整体预校验，部分启动失败回收资源，单次 CRM 调用失败不停止 Supply Chain。
- [ ] 受控重启更新配置；不做热加载或多宿主共享 SQLite 写入。

提交 B：manifest 与工具路由。

- [ ] 安装 manifest 为批准上下文的工具名并集，控制名去重；不是用户可见工具并集。
- [ ] search/describe/factory/invoke 均依据可信上下文和当前授权；未知或不一致上下文拒绝，无 main 回落。
- [ ] 拒绝大小写工具名冲突、额外工具、generation/reception 错误绑定。
- [ ] status/result 复查用户、租户、会话、profile 的 operation 归属；CRM 不增加取消、审批、重提权限。

提交 C：原生宿主关联与接待路由。

- [ ] 在固定原生宿主证明 Agent/Session/Run 贯穿可见性、创建、调用钩子；不足时只加中立关联补丁并重建签名，模型参数不得供给身份。
- [ ] Supply Chain 旧路由固定 main；新增固定 crm 路由及独立 ingress 凭证，请求体不得切换 Agent。
- [ ] 禁止未批准的跨 Agent 历史、记忆、委派和原生工具。

门槛：同一原生宿主并发 main/crm，工具、凭证、会话、账本和 operation 不串用。

### P3：Deployment/BFF/只读 Workflow

- [ ] 增量扩展实际配置，保留 Supply Chain 包、pin、权限、模型、generation 和用户 MCP key 解析，不用示例整包覆盖。
- [ ] 同 composition 增加 crm offer，复用逻辑 Agent 独立 generation 路由。
- [ ] CRM Provider 仅 crm.get_case、crm.get_case_context、crm.get_case_evidence、crm.get_account_capabilities；MCP allowlist 原集合并四个 CRM 读工具，不含底层写及证据刷新。
- [ ] Attestation 支持精确原三 Provider 或含 CRM 的四 Provider 集合，拒绝重复、未知、额外 Provider。
- [ ] 发布入口先编译、核预期摘要、确认零 WRITE，再写 Registry；负向测试证明失败前零写入。
- [ ] 共享 BFF 数据库、ERP JWT/Redis 和用户 MCP key；crm-reception.v1 指向共享宿主 CRM 固定入口。
- [ ] 各 generation 单独签名、物化多上下文 manifest；SC generation 内容不变。

门槛：目录、Workflow、凭证和授权闭包一致，非法材料不得先写 Registry 再失败。

### P4：emf-ai CRM 接线

- [ ] 按归属保存 CRM 页面、conversation API/store 和测试 WIP，不夹带 Financial、SC 或其他页面改动。
- [ ] 维持 /api/crm/v2/openclaw/**，不回退旧聊天或演示建议。
- [ ] 复用 ERP 登录协议，BFF JWT 仅内存；前端不接触签名、Redis 密钥或 MCP key。
- [ ] 测会话/同请求恢复、Case 输入、结果/错误展示、用户切换清理；Permission select 保留为偏好，不提供权限。
- [ ] 核已发布前端接线；必要时发布冻结版本，做 SC 页面回归。

门槛：真实 BFF，无自动重复提交、旧身份残留或 mock 成功。

### P5：本地候选包和原生集成验证

- [ ] focused → 包级 → 相关完整门禁；不运行业务写入套件。
- [ ] Adapter Node 24.19.0/pnpm 11.19.0；emf-ai 使用自身锁定工具链。
- [ ] 补跑默认 pnpm test 未覆盖的目录/manifest/持久化/reception/生命周期及多 Agent 测试。
- [ ] 最终 wheel、Adapter 包及固定宿主安装态测试；源码、输出、签名匹配。
- [ ] 浏览器在本机运行，Dev 服务器仅部署服务，不要求 Chrome。
- [ ] 支持目标用统一 runner，否则仓库入口并记录真实命令、解释器、退出码和统计。

门槛：必需门禁全部通过；mock 上下文不替代原生隔离证据。

## 3. 接口与版本

| 边界 | 约定 |
| --- | --- |
| Gateway | wire v1，不添加 WRITE Offer |
| 宿主配置 | ebizhub.openclaw-host/v2，多上下文；不是 Gateway wire v2 |
| 上下文绑定 | agent/profile、generation 信任、reception 摘要、状态路径和固定 ingress |
| 单 Agent 兼容 | 旧配置归一为 main，旧路由及行为保持 |
| 身份 | 不改 CredentialReply wire；host 字段仅关联，BFF 运行绑定/授权提供依据 |
| CRM | offer crm-case-advice、tool crm_case_advice、输入仅 case_id |
| Workflow | crm-case-advise-on-demand@1 |
| 摘要 | 54f9a85048b024ca6b488abce7fc2bd3adb106a363a1c58b8ef4422e701a4095 |
| Scopes | workflow:start、runtime:admission、crm.case.read、crm.context.read、crm.evidence.read、crm.capability.read、crm.compute、crm.preview |
| 结果 | schema 校验后白名单投影，草稿/引用/阻塞，明确“建议回复，未发送” |

八 Runtime scopes 不等于八 MCP tools；只需四个 CRM read tools。不新增数据库结构或 Java API；迁移、后端改造或凭证协议变化需记录差异并阻断，不自行扩范围。

## 4. 测试矩阵

| 场景 | 结果 |
| --- | --- |
| 旧单 Agent | SC discover/invoke/history/result 保持 |
| 多 Agent/用户并发 | 工具/状态互不串用 |
| 假 Agent/profile/generation、缓存调用/猜名 | 无越权 |
| operation 跨用户/租户/profile | status/result 拒绝 |
| 撤权/过期/key 失效 | 无缓存借权 |
| 错摘要/额外 Provider/工具冲突/manifest 错 | 启用前失败 |
| Fake CRM | ADVISED/BLOCKED/TAKEOVER/技术失败 |
| 真实 CRM | 四读/模型/投影可证明；BLOCKED 不算建议成功 |
| 超时/响应丢失/重启 | 不自动重复提交，CRM 单请求不拖停 SC |
| 页面 | 刷新/返回/切身份/恢复/错误/权限正确 |
| 写边界 | 不发送/转派/备注/加急/刷新证据，业务 WRITE=0 |
| 回退 | SC 可用，已受理 CRM 记录保留 |

真实验收默认 bestptv/cid 222260、合成 Case case-dev-20261007-001，先核仍存在且为测试数据。负向权限测试使用实际无权限/跨租户身份；缺少身份标记未完成，授权账号不得替代。

## 5. 发布、回退与交付

### P6：维护发布与真实页面验收

顺序固定：冻结 SHA/包/镜像/锁/配置/宿主证明/generation/reception/DB heads → 确认维护窗口 → 关闭新 admission、处理在途 → 发布只读 Registry（CRM 关闭） → 必要 Runtime/BFF 更新及 SC 验证 → 更新现有宿主多 Agent 包、配置、manifest → main 验证 → crm 验证 → 同源代理/必要前端 → 仅既定租户启用 → 本机真实页面/权限验收 → 运行版本复核。

不强杀在途任务，不启动长期 CRM 专用宿主，不切换空 crm_runtime/crm_bff，旧独立实例端口方案作废。仅留脱敏请求统计、operation/execution 关联，不留客户正文、Token 或敏感 trace。

### P7：回退演练和交付

- [ ] CRM 失败：关 admission、核在途，恢复 main 单上下文配置/manifest，必要时受控重启。
- [ ] SC/认证回归：恢复精确 Runtime+BFF+Adapter+host+config 组合，不只 JS。
- [ ] 不删 DB/ledger/volume，不自动降 DB；旧包无法恢复 CRM 时先核查。
- [ ] 演练后恢复最终候选，重新验收，不复用切换前证据。
- [ ] 交付 feature/完整 SHA/推送状态（rongyu.yuan）、Adapter bundle/包摘要、Jenkins 构建、运行版本/入口、脱敏清单、命令/版本/退出码/统计/未完成项、回退步骤和演练。

证据：C:\ebizhub\.local\crm-shared-openclaw-dev-20261010\。
只有真实页面建议、权限隔离、SC 回归和回退全部完成，才能标记 CRM 只读 Dev 验收完成。

## 6. 执行记录

- 2026-10-10：已批准计划；保存六仓 WIP 快照，无修改原 WIP、无服务重启。当前快照见本地证据目录。
- P0 尚未完成：需本轮 Dev 运行版本、回退包及 Adapter WIP 归属证据。
- P1–P7 尚未完成；以前独立实例证据不能当本次共享架构验收。
