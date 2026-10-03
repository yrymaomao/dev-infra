# CRM / Supply Chain 用户登录与 MCP Key 部署变更

本轮仅修改 Deployment，不新增数据库迁移，不修改 MCP Server 或业务 Workflow。

## 登录配置

两个入口共用一个 ERP Redis 连接池。Supply Chain 使用现有 Bearer token，
CRM 保留 `_token_` 和 120 秒内部 JWT。登录解析不需要启用 CRM Workbench。

| 环境变量 | 内容 |
| --- | --- |
| `BFF_ERP_SESSION_REDIS_URL_FILE` | 受控文件路径；文件包含 ERP 登录 Redis URL，不能指向 Runtime 自己的 Redis |
| `BFF_ERP_SESSION_JWT_SECRET_FILE` | 受控文件路径；文件包含现有 ERP/Framework 的签名密钥，不生成新密钥 |
| `BFF_ERP_SESSION_TIMEOUT_SECONDS` | 默认 2 秒 |
| `MCP_USER_REF_SIGNING_KEY` | BFF 与 Gateway 共用的用户引用签名密钥，至少 32 字节 |
| `BFF_SUPPLY_CHAIN_USER_BOUND_CREDENTIALS_ENABLED` | BFF 与 Gateway 同时开启 |

Redis 来源应核对 ERP-ai 的 Nacos 主配置 `erp-ai` 的 `redis` 节点。
JWT 来源需核对当前 ERP/Framework 实现；不能假定 Nacos 中一定有该项。
上述变量是部署机上的文件引用，不是 Nacos DataId。配置值、登录 token 和 MCP Key
不得提交 Git、写入测试日志或发送到模型。

## Provider 配置

使用 `config/deployment.example.json` 或 CRM 对应示例中的 `user_mcp_key_query`：
Nacos `server_addresses`、`namespace`、`group_name`、`allowed_hosts`，以及
`signing_secret_name` 和 `timeout_seconds`。可选 Nacos用户名/密码使用成对的
`username_secret_name`、`password_secret_name`，并声明到 `secrets.allowed_env`。

服务名固定 `McpServer`，仅调用命名查询，不读取其他业务 DataId、不注册服务。
发现结果缓存 5 秒，只选择 healthy、enabled、正权重实例。Key 不缓存。
请求固定为 `POST /erp/feign/mcp/key/query/aggr-key`，参数是可信 `cid/uid`；
成功 envelope 的 `result.mcpKey` 由现有 Adapter 原样发到 `X-Mcp-Key`。

Nacos 及 AGGR 查询地址都必须符合 HTTPS/回环 HTTP 策略，证书和主机名必须有效。
远端明文 HTTP 无受控接入时记录真实验收阻塞，不修改安全策略。

## 清理与恢复

不再使用独立 Broker、手工 uid/cid 引用绑定文件、`BFF_CRM_SERVICE_URL`
或 CRM `session-principal` HTTP 接口。保留模型服务的通用 secret 解析。

新执行只使用可信用户的签名引用，不回退全局 Key。旧报告缺引用时不继续派发，
也不伪造 Runtime FAILED 状态；其问题不会终止其他报告的状态核对。
已创建执行不重写凭据引用。发布前应核对在途旧任务，必要时按原制品完成或回滚。

## 更新顺序与成功标准

1. 备份原 Deployment wheel、配置摘要和运行 pins。
2. 构建并安装同一 feature 提交的 wheel，核对实际加载路径及 SHA-256。
3. 配置 ERP 登录文件和 Nacos 发现；BFF 与 Gateway 使用同一签名密钥。
4. 在途任务结束后只重启 BFF 和装配宿主，不更换 Runtime/Agent 制品。
5. 正式登录用户通过两个入口鉴权；Key 查询、tools/list、真实工具调用及报告均可用。
6. 越权、失效登录态、无 Key 必须拒绝；确认不再调用旧鉴权边界。

本地假服务用例通过不能代替第 5 步真实 dev 验收。提交对应的测试报告须分别记录
本地结果和真实环境阻塞，不能把业务 BLOCKED 或网络失败写成完整报告成功。
