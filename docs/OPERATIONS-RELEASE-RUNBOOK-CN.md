# MyEMS 生产运维与发布手册

## 1. 文档信息

| 项目 | 内容 |
| --- | --- |
| 文档名称 | MyEMS 生产运维与发布手册 |
| 文档版本 | v1.4 |
| 生效日期 | 2026-10-18 |
| 适用系统 | MyEMS Docker Compose 生产环境 |
| 适用目录 | `/home/ubuntu/myems-complete` |
| 运维入口目录 | `/home/ubuntu/myems-complete/others` |
| 维护责任 | 生产运维负责人、应用发布负责人、数据库负责人 |
| 敏感信息管理 | 账号密码、Token、密钥不写入本仓库，应保存在密码库或运维台账 |

> 集社云接入版本的发布以[第20节](#20-集社云电表接入版本上线说明2026-10)为准，包含生产变量、镜像、增量迁移、调度、验收与回退。第12节中的历史版本标签及第19节的产品碳足迹发布剧本不适用于本次接入；本节文档更新不代表已经批准或执行生产上线。

## 2. 修订记录

| 版本 | 日期 | 修改人 | 说明 |
| --- | --- | --- | --- |
| v1.0 | 2026-04-16 | GitHub Copilot | 初版，覆盖发布、停机、恢复、回滚、数据对账 |
| v1.1 | 2026-04-28 | GitHub Copilot | 新增纯镜像生产部署模板、离线镜像交付要求、EMQX 与 myems-mqtt 初始化指引 |
| v1.2 | 2026-05-07 | GitHub Copilot | 补充 GitHub Actions 预构建、GHCR 在线验证、离线镜像包交付、分批上线与验证 SOP |
| v1.3 | 2026-05-08 | GitHub Copilot | 补充当前仓库 GitHub Actions 触发规则、artifact 命名、分支到 tag 的发布路径与本版本上线剧本 |
| v1.4 | 2026-10-18 | GitHub Copilot | 增加集社云9台试点版本的生产配置、镜像交付、增量迁移、分阶段调度、验收边界及回退要求 |

## 3. 文档目的与适用范围

本文档用于指导 MyEMS 生产环境的日常发布、停机维护、异常恢复、回滚与数据对账，目标是：

1. 尽量把版本发布安排在工作时间之后执行。
2. 在升级应用的同时，尽量减少对用户访问的影响。
3. 在涉及计量数据采集和外部接口接入时，优先控制数据丢失、断传和漏算风险。
4. 为值班人员提供可直接复制执行的命令模板和检查清单。

本文档适用于当前基于 Docker Compose 部署的 MyEMS 生产环境，也适用于后续同类环境复制使用。

## 4. 当前生产环境概况

### 4.1 当前部署结构

当前生产环境采用宿主机 MySQL + Docker Compose 应用服务模式。

当前核心运行面：

1. `emqx`
2. `web`
3. `admin`
4. `api`
5. `aggregation`
6. `cleaning`
7. `normalization`
8. `modbus_tcp`
9. `myems_mqtt`

Compose 文件位置：

`/home/ubuntu/myems-complete/others/docker-compose-on-linux.yml`

共享上传目录：

`/myems-upload`

### 4.2 当前端口规划

| 端口 | 服务 | 说明 |
| --- | --- | --- |
| 80 | `web` | 前台用户访问入口 |
| 8001 | `admin` | 管理后台入口 |
| 8000 | `api` | API 服务入口 |
| 1883 | `emqx` | MQTT Broker 入口，仅允许可信网络或开启 TLS 后暴露 |
| 18083 | `emqx` | EMQX Dashboard 入口，仅允许运维管理访问 |
| 3306 | MySQL | 数据库服务，仅允许宿主机和容器访问，不应对公网开放 |

### 4.3 当前服务特性

1. `web` 与 `admin` 为无状态前端入口，停机会直接影响登录与页面访问。
2. `api` 为系统统一业务入口，停机会导致前后台和外部调用全部不可用。
3. `modbus_tcp` 为实时轮询采集服务，停机窗口内会形成潜在数据盲区。
4. `emqx` 为 MQTT Broker，停机会导致 MQTT 设备或外部网关无法继续上送数据。
5. `myems_mqtt` 为 MQTT 入库适配服务，停机会导致已进入 Broker 的 MQTT 消息无法继续写入 MyEMS 历史表。
6. `cleaning`、`normalization`、`aggregation` 为后台处理服务，停机后通常可以通过恢复后的追平继续补处理。
7. 生产环境默认使用纯镜像模板 `others/docker-compose-on-linux.image.yml`，只允许执行 `docker compose pull` 或 `docker load` 后再 `up -d --no-build`。
8. `others/docker-compose-on-linux.yml` 仅用于开发、预发或经过审批的应急现场重建，不作为低内存生产环境的常规升级入口。

## 5. 系统架构与服务职责

### 5.1 服务职责说明

| 服务 | 主要职责 | 是否直接影响用户访问 | 是否直接影响数据采集 | 是否支持延迟恢复后追平 |
| --- | --- | --- | --- | --- |
| `emqx` | MQTT Broker，接收外部采集网关或设备上送消息 | 否 | 是 | 取决于客户端重传与会话策略 |
| `web` | 前台页面展示、看板、报表入口 | 是 | 否 | 不涉及 |
| `admin` | 后台管理、数据源与配置维护 | 是 | 间接 | 不涉及 |
| `api` | 登录、查询、配置、报表、第三方业务入口 | 是 | 间接 | 不涉及 |
| `modbus_tcp` | 实时采集 Modbus TCP 设备数据 | 否 | 是 | 默认否 |
| `myems_mqtt` | 订阅 MQTT Topic 并写入 MyEMS 历史表 | 否 | 是 | 取决于 Broker 保留、QoS 和客户端重投策略 |
| `cleaning` | 历史数据清洗、坏点标记 | 否 | 否 | 是 |
| `normalization` | 原始累计量归一化计算 | 否 | 否 | 是 |
| `aggregation` | 报表统计、聚合、碳排、账单计算 | 否 | 否 | 是 |
| MySQL | 业务数据、历史数据、配置数据存储 | 是 | 是 | 否 |

### 5.2 依赖关系说明

1. `web` 与 `admin` 依赖 `api`。
2. `api`、`modbus_tcp`、`myems_mqtt`、`cleaning`、`normalization`、`aggregation` 全部依赖 MySQL。
3. `myems_mqtt` 依赖 `emqx`、数据源、点位、Topic 绑定和网关 Token 配置。
4. `normalization` 依赖历史数据存在。
5. `aggregation` 依赖 `normalization` 的输出结果。
6. `modbus_tcp` 依赖网关、数据源、点位配置以及设备网络可达性。

## 6. 发布影响矩阵

| 停机对象 | 用户可见影响 | 数据风险 | 恢复后是否可追平 | 发布说明 |
| --- | --- | --- | --- | --- |
| `emqx` | 用户未必立刻感知，但 MQTT 入站中断 | 取决于设备 QoS、保留和重传策略，存在断传风险 | 取决于客户端补发能力 | 高风险发布项 |
| `web` | 前台无法打开或页面报错 | 无直接数据丢失 | 不涉及 | 可单独发布 |
| `admin` | 管理后台不可登录或不可配置 | 无直接数据丢失 | 不涉及 | 可单独发布 |
| `api` | 前后台全部不可用，第三方接口失败 | 若外部接口依赖 API，存在断传风险 | 不涉及 | 建议与前端错峰发布 |
| `cleaning` | 用户通常无直接感知 | 数据质量处理延迟 | 是 | 可短暂停机 |
| `normalization` | 报表与统计延迟更新 | 归一化结果滞后 | 是 | 可短暂停机 |
| `aggregation` | 统计、碳排、账单延迟更新 | 聚合结果滞后 | 是 | 可短暂停机 |
| `modbus_tcp` | 用户未必立刻感知，但实时采集停止 | 停机期间可能永久丢失实时采集点 | 默认否 | 高风险发布项 |
| `myems_mqtt` | 用户未必立刻感知，但 MQTT 数据不再入库 | Broker 已接收但未落库的数据可能延迟或丢失 | 取决于 Broker 会话和补发策略 | 高风险发布项 |
| MySQL | 全系统不可用 | 高风险 | 否 | 非必要禁止在正常发布中改动 |

## 7. 发布分级与时间窗口策略

### 7.1 发布分级

#### A 类：前端发布

适用范围：

1. `web` 静态资源调整
2. `admin` 静态资源调整
3. 样式、菜单、页面交互调整

发布要求：

1. 可单独发布。
2. 推荐在工作时间后执行。
3. 不应影响 `modbus_tcp`。

#### B 类：API 发布

适用范围：

1. API 逻辑调整
2. 登录、权限、接口字段、校验规则变更
3. 外部系统 HTTP/API 交互逻辑调整

发布要求：

1. 必须在工作时间后执行。
2. 应提前通知后台用户和接口调用方。
3. 如果不涉及采集链路，禁止顺带重启 `modbus_tcp`。

#### C 类：后台计算服务发布

适用范围：

1. `cleaning`
2. `normalization`
3. `aggregation`

发布要求：

1. 可与 API 分开发布。
2. 允许短暂停机。
3. 恢复后应重点验证是否开始追平。

#### D 类：采集链路发布

适用范围：

1. `modbus_tcp`
2. `emqx`
3. `myems_mqtt`
4. 网关、数据源、点位、采集逻辑调整
5. 第三方计量数据接口接入逻辑变更

发布要求：

1. 必须在工作时间后执行。
2. 必须设置停机时长目标。
3. 必须执行停机前后时点核对。
4. 必须具备明确恢复策略。
5. 若使用 MQTT，必须确认 EMQX 与 `myems_mqtt` 的镜像版本、配置和 `.env` 一致。

#### E 类：数据库与配置发布

适用范围：

1. `.env` 配置变更
2. 数据库结构变更
3. 连接参数、时区、端口、外部接口凭证变更

发布要求：

1. 必须提前备份。
2. 必须确认受影响服务的预构建镜像、镜像标签和 `docker-images.env` 已准备完成。
3. 必须准备回滚方案。

### 7.2 发布时间窗口

推荐窗口：

1. 常规发布：工作日 19:00 以后。
2. 高风险发布：22:00 以后，并安排值守。
3. 禁止窗口：工作日高峰、月底结算、重点保障时段。

## 8. 停机与恢复总体策略

### 8.1 总体原则

1. 不建议默认全栈停机发布。
2. 采集链路必须最后停、最先恢复。
3. 能通过重启完成的，不做全量重建。
4. 涉及配置构建变更时，优先缩小重建范围。
5. 若发布窗口超出预估，优先考虑回滚而不是持续延长停机。

### 8.2 推荐发布顺序

1. `web`
2. `admin`
3. `api`
4. `cleaning`
5. `normalization`
6. `aggregation`
7. `emqx`
8. `myems_mqtt`
9. `modbus_tcp`

### 8.3 推荐恢复顺序

1. MySQL
2. `api`
3. `web`
4. `admin`
5. `emqx`
6. `myems_mqtt`
7. `modbus_tcp`
8. `cleaning`
9. `normalization`
10. `aggregation`

### 8.4 预构建镜像发布策略

适用：生产环境使用 Docker Compose 启动，但不希望在生产机现场执行长时间镜像构建。

原则：

1. 在独立构建机、CI 或预发环境中提前异步构建镜像。
2. 构建完成后推送到企业镜像仓库，或导出离线镜像包。
3. 生产机只执行 `docker compose pull` 或 `docker load`，再执行 `docker compose up -d --no-build`。
4. 低内存生产环境不应在发布窗口现场构建镜像。
5. 如遇镜像仓库不可用，应使用离线镜像包而不是切回现场构建。

推荐流程：

1. 在构建环境打包 `api`、`admin`、`web`、`aggregation`、`cleaning`、`normalization`、`modbus_tcp`、`myems_mqtt` 镜像。
2. 为 `emqx` 明确固定镜像标签，或构建并推送自定义 EMQX 镜像。
3. 将镜像标签写入 `others/docker-images.env`，由生产模板 `others/docker-compose-on-linux.image.yml` 使用。
4. 若生产网段不能访问镜像仓库，额外输出离线镜像包，例如 `docker save -o myems-images-<release>.tar ...`。
5. 生产发布时执行 `docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml pull`，或先 `docker load` 再 `up -d --no-build`。

### 8.5 当前仓库 GitHub Actions 规则说明

当前仓库已存在工作流：

`/.github/workflows/prebuild-images.yml`

该工作流的实际行为为：

1. 支持手动触发 `workflow_dispatch`。
2. 也会在推送 tag 且 tag 名匹配 `v*` 或 `release-*` 时自动触发。
3. 默认镜像前缀为 `ghcr.io/<repository_owner_lower>/myems`。
4. 对当前仓库 owner `tian0605` 来说，默认镜像前缀即：`ghcr.io/tian0605/myems`。
5. 工作流会构建并推送以下镜像：`api`、`aggregation`、`cleaning`、`modbus-tcp`、`mqtt`、`normalization`、`admin`、`web`。
6. 工作流不会构建 EMQX 镜像，只会把 `emqx_image` 输入值写入 `docker-images.env`。
7. 若不显式传入 `emqx_image`，当前默认值是 `emqx/emqx:latest`。

执行要求：

1. 生产发布时不要依赖 `emqx/emqx:latest`，必须在触发 Actions 时显式指定固定版本，例如 `emqx/emqx:5.8.6`。
2. 生产发布优先使用 tag 触发，而不是直接依赖 SHA 默认 tag。
3. 推荐统一使用 release tag，例如 `v2026.05.08-prod.1`。

产物命名规则：

1. Actions artifact 名称为 `prebuild-metadata-<release_tag>`。
2. artifact 内包含：`docker-images.env` 与 `image-manifest.txt`。
3. `image-manifest.txt` 会记录每个镜像的完整引用，可直接作为发布核对单使用。

## 9. 外部计量数据接口专项策略

### 9.1 Modbus TCP 接口

特性：

1. 当前实现为实时轮询采集。
2. 停机窗口内默认不会自动补采。
3. 若现场设备不支持历史回读，则停机时段即潜在数据盲区。

发布要求：

1. 非必要不重启 `modbus_tcp`。
2. 若必须停机，必须记录停机开始时间、停机结束时间、关键点位最新采集时间。
3. 恢复后必须核对最新时间戳是否继续前进。

### 9.2 第三方 HTTP/API 接口

处理原则：

1. 如果对方支持补推、补拉、重放，则在恢复后执行补传和对账。
2. 如果对方不支持补传，则必须要求对方在发布窗口内暂停推送，或由本方提供缓冲机制。
3. 如果双方都不支持补传，则不允许执行长时间停机发布。

## 10. 标准发布 SOP

### 10.1 发布前 SOP

目的：

在正式执行前确认环境健康、窗口合规、镜像产物可用、回滚可用、外部接口已协调。

前提：

1. 发布说明、数据库脚本、镜像标签、回滚方案已准备完成。
2. 关键责任人已到位。
3. 发布通知已发送。

步骤：

1. 确认发布时间在批准窗口内。
2. 确认本次属于 A/B/C/D/E 哪一类发布。
3. 确认是否涉及 `modbus_tcp`、`emqx`、`myems_mqtt` 或外部接口。
4. 记录当前 Git 提交号。
5. 确认本次目标版本对应的 Git tag 或 release tag。
6. 记录容器状态、关键日志、关键时间戳。
7. 备份数据库或相关配置。
8. 确认 `.github/workflows/prebuild-images.yml` 已在远程仓库可见且本次需要的镜像都已预构建完成。
9. 从 Actions artifact 下载 `docker-images.env` 与 `image-manifest.txt`，或用 `scripts/render-docker-images-env.sh` 重新生成。
10. 预演回滚命令。

验证：

1. `docker compose ps` 状态正常。
2. Web、Admin、API 当前可访问。
3. 数据库连接正常。
4. 若涉及采集链路，最新采集时间在正常推进。
5. 若涉及 MQTT 链路，`emqx` Dashboard 与 `myems_mqtt` 最近日志无持续异常。
6. 目标镜像在 GHCR 或目标镜像仓库中可见。
7. `docker-images.env` 中的镜像标签与本次 release tag 一致。

失败处理：

1. 若基线环境已异常，禁止继续发布。
2. 若回滚方案未准备，禁止继续发布。
3. 若镜像产物未准备完成，禁止进入生产发布。

### 10.2 CI 预构建 SOP

目的：

在生产窗口前完成镜像构建、推送与标签文件生成，避免生产机现场构建。

前提：

1. 代码已合并到发布分支或主干。
2. 已确认本次 release tag。
3. 仓库 `Actions` 权限允许 `Read and write permissions`。

步骤：

1. 打发布 tag，例如 `v6.3.6-prod`，或手动触发 `Prebuild Release Images` 工作流。
2. 等待工作流完成 `prepare`、`build`、`package-metadata` 三个阶段。
3. 在 Packages 页面确认 `api`、`aggregation`、`cleaning`、`modbus-tcp`、`mqtt`、`normalization`、`admin`、`web` 镜像都已生成。
4. 下载 artifact：`prebuild-metadata-<release_tag>`，并解压得到 `docker-images.env` 与 `image-manifest.txt`。
5. 核对 `docker-images.env` 中的镜像前缀、release tag、EMQX 镜像是否正确。
6. 若本次通过 `workflow_dispatch` 触发，务必填写 `emqx_image`，不要让生产环境落到 `emqx/emqx:latest`。
7. 在网络较好的验证机或预发机执行至少一轮 `web`、`admin` 在线拉取验证。

验证：

1. Actions 工作流状态为成功。
2. `docker-images.env` 已生成且与 release tag 一致。
3. 至少一个验证环境已完成 `web` 和 `admin` 的 `HTTP 200` 检查。
4. `image-manifest.txt` 中各镜像引用与计划上线版本一致。

失败处理：

1. 若单个镜像构建失败，修复后重新触发工作流。
2. 若 artifact 与镜像标签不一致，禁止进入生产发布。

### 10.3 生产发布路径选择 SOP

目的：

根据生产机资源和网络状况，在“在线拉取”与“离线镜像包”之间选择正确路径。

决策原则：

1. 若生产机到 GHCR 或企业镜像仓库下载稳定且发布窗口宽裕，可使用在线拉取。
2. 若生产机低内存、低带宽、下载极慢，默认使用离线镜像包。
3. 不得因在线拉取过慢而临时切回生产机现场 `build`。

推荐结论：

1. GitHub Actions 负责预构建和生成 `docker-images.env`。
2. 正式生产发布优先使用离线镜像包。
3. 在线拉取主要用于预发验证、轻量服务验证或临时应急。

### 10.4 生产发布执行 SOP

目的：

按最小影响原则执行镜像上线，并把停机时间控制在可接受范围内。

步骤：

1. 将本次发布使用的 `docker-images.env` 放入生产机 `others/` 目录。
2. 若走在线拉取，先执行分批 `pull`。
3. 若走离线镜像包，先执行 `docker load`，确认本地镜像标签存在。
4. 按顺序分批上线：`web` -> `admin` -> `api` -> `cleaning normalization aggregation` -> `emqx myems_mqtt` -> `modbus_tcp`。
5. 每一批启动后立即执行状态、日志和 HTTP 检查，不要一次性重启全部服务。
6. 若本次只涉及 `api`、`admin`、`web` 和数据库脚本，禁止顺带重启 `modbus_tcp`。
7. 若涉及 `emqx`、`myems_mqtt` 或 `modbus_tcp`，必须记录停机前后的采集时间戳。

### 10.4.1 GitHub Actions 驱动的标准发布路径

适用：

1. 代码已经合并或已确认当前分支提交可发布。
2. 镜像构建在 GitHub Actions 完成。
3. 生产机只负责拉取或装载镜像并重启服务。

推荐路径：

1. 先把业务代码推送到远端分支。
2. 再创建并推送 release tag。
3. 等待 `Prebuild Release Images` 完成。
4. 下载 `prebuild-metadata-<release_tag>` artifact。
5. 将 artifact 中的 `docker-images.env` 放到生产机 `others/` 目录。
6. 按影响范围分批在生产机执行 `pull` / `up -d --no-build`。

不推荐路径：

1. 直接在生产机现场 build。
2. 使用未固定版本的 EMQX 镜像。
3. 在镜像尚未完成推送时提前执行生产升级。

验证：

1. 每一批服务 `up -d --no-build` 后状态正常。
2. `web` 返回 `HTTP 200`。
3. `admin` 返回 `HTTP 200`。
4. `api` `/version` 或健康接口可访问。

失败处理：

1. 若单批失败，先回滚当前批次，不扩大影响范围。
2. 若停机时长超出预设上限，立即停止后续批次并启动回滚。

### 10.5 发布后 SOP

目的：

确认服务恢复、采集恢复、报表恢复和数据完整性。

步骤：

1. 检查容器状态。
2. 检查 Web、Admin、API 可用性。
3. 检查 `modbus_tcp` 是否恢复采集。
4. 检查 `emqx` 与 `myems_mqtt` 是否恢复正常。
5. 检查 `cleaning`、`normalization`、`aggregation` 是否恢复运行。
6. 检查关键时间戳、关键点位、关键报表。
7. 完成数据对账记录。

验证：

1. 全部容器状态正常。
2. 登录与主要页面访问正常。
3. 采集时间继续推进。
4. MQTT Dashboard 与入库日志正常。
5. 归一化、聚合恢复追平。

失败处理：

1. 若用户访问异常，优先恢复 API 与前端。
2. 若采集未恢复，立即进入应急恢复流程。

## 11. 发布前检查表

执行方式：发布前逐项勾选，不得口头替代。

| 序号 | 检查项 | 结果 | 备注 |
| --- | --- | --- | --- |
| 1 | 发布时间在批准窗口内 | [ ] | |
| 2 | 已识别本次发布分级 | [ ] | |
| 3 | 已确认是否涉及采集链路 | [ ] | |
| 4 | 当前 `docker compose ps` 正常 | [ ] | |
| 5 | MySQL 运行正常 | [ ] | |
| 6 | Web 当前可访问 | [ ] | |
| 7 | Admin 当前可访问 | [ ] | |
| 8 | API 当前可访问 | [ ] | |
| 9 | 关键日志无持续性异常 | [ ] | |
| 10 | 已备份相关数据库/配置 | [ ] | |
| 11 | 已确认回滚版本可用 | [ ] | |
| 12 | 已通知相关业务与值班人员 | [ ] | |
| 13 | 若涉及外部接口，已完成窗口协调 | [ ] | |
| 14 | 若涉及采集链路，已记录最新采集时间 | [ ] | |
| 15 | 若涉及 MQTT，已确认 `myems-mqtt/.env`、Broker 配置和镜像标签 | [ ] | |
| 16 | 若走预构建方案，目标镜像已在仓库可拉取 | [ ] | |
| 17 | 已从 Actions 下载 `docker-images.env` 与 `image-manifest.txt` | [ ] | |
| 18 | 已确认在线拉取或离线镜像包发布路径 | [ ] | |
| 19 | 若走离线方案，镜像 tar 包已生成并可校验 | [ ] | |
| 20 | 若走在线方案，已在验证机完成 `web`、`admin` 拉取与访问检查 | [ ] | |

## 12. 发布操作命令清单

以下命令均在当前生产目录下执行，且默认使用纯镜像模板：

```bash
cd /home/ubuntu/myems-complete/others
# 把本次发布的 docker-images.env 放到当前目录
```

### 12.1 生成或刷新镜像标签文件

适用：需要根据 release tag 重建 `docker-images.env`，或验证 artifact 内容。

```bash
cd /home/ubuntu/myems-complete
bash scripts/render-docker-images-env.sh \
  v6.3.6-prod \
  ghcr.io/tian0605/myems \
  emqx/emqx:5.8.6 \
  others/docker-images.env

sed -n '1,20p' others/docker-images.env
```

### 12.1.1 推送分支并创建发布 tag

适用：当前版本已经在本地通过验收，需要推送远端并触发 GitHub Actions 预构建。

```bash
cd d:/VSCode/myems_development_enterprise-isolation-v2

# 1. 推送当前功能分支
git push origin feature/enterprise-isolation-v3

# 2. 创建发布 tag，建议使用带日期和序号的 tag
git tag v2026.05.08-prod.1

# 3. 推送 tag，自动触发 Prebuild Release Images
git push origin v2026.05.08-prod.1
```

说明：

1. 若 tag 推送后 Actions 自动触发，则无需手动再点一次 workflow。
2. 若希望手动指定 `emqx_image`，可不先推 tag，而是到 GitHub Actions 页面用 `workflow_dispatch` 触发，并填写：
  - `release_tag`: `v2026.05.08-prod.1`
  - `registry_prefix`: `ghcr.io/tian0605/myems`
  - `emqx_image`: `emqx/emqx:5.8.6`
3. 若选择手动触发，仍建议最终补一个同名 tag 以便版本追踪。

### 12.1.2 下载并核对 Actions artifact

```bash
# 在 GitHub Actions 页面下载：prebuild-metadata-v2026.05.08-prod.1
# 解压后应至少看到：
#   docker-images.env
#   image-manifest.txt
```

核对要求：

1. `docker-images.env` 中各镜像 tag 必须都是 `v2026.05.08-prod.1`。
2. `MYEMS_EMQX_IMAGE` 必须是固定版本，例如 `emqx/emqx:5.8.6`。
3. `image-manifest.txt` 中的 registry 前缀必须为 `ghcr.io/tian0605/myems`。

### 12.2 场景一：仅前端发布

适用：仅 `web` 或 `admin` 代码变更。

```bash
cd /home/ubuntu/myems-complete/others
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml pull web admin
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml up -d --no-build web
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml ps web
curl -I --max-time 15 http://127.0.0.1/

docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml up -d --no-build admin
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml ps admin
curl -I --max-time 15 http://127.0.0.1:8001/
```

### 12.3 场景二：API 与后台服务发布

适用：`api`、`cleaning`、`normalization`、`aggregation` 变更，不涉及采集链路。

```bash
cd /home/ubuntu/myems-complete/others
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml pull api cleaning normalization aggregation
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml up -d --no-build api
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml ps api
curl -i --max-time 15 http://127.0.0.1:8000/version | sed -n '1,20p'

docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml up -d --no-build cleaning normalization aggregation
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml ps cleaning normalization aggregation
```

### 12.4 场景三：MQTT 链路发布

适用：`emqx`、`myems_mqtt`、MQTT Topic/数据源/点位接入逻辑变更。

```bash
cd /home/ubuntu/myems-complete/others

# 1. 检查发布前日志
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml logs -t --since=10m emqx
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml logs -t --since=10m myems_mqtt

# 2. 若使用预构建镜像，先拉取
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml pull emqx myems_mqtt

# 3. 重新部署 MQTT 链路
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml up -d --no-build emqx myems_mqtt

# 4. 检查状态
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml ps emqx myems_mqtt
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml logs -t --since=5m emqx
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml logs -t --since=5m myems_mqtt
```

### 12.5 场景四：配置变更但仍使用预构建镜像

适用：`.env`、`nginx.conf`、EMQX 参数、镜像标签变更，且新镜像已提前构建完成。

```bash
cd /home/ubuntu/myems-complete/others
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml pull api admin web cleaning normalization aggregation emqx myems_mqtt modbus_tcp
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml up -d --no-build api admin web cleaning normalization aggregation emqx myems_mqtt modbus_tcp
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml ps
```

警告：该命令影响范围大，执行前必须确认本次是否真的需要重启全部服务。

### 12.6 场景五：采集链路发布

适用：`modbus_tcp`、点位采集逻辑、外部计量接入逻辑变更。

```bash
cd /home/ubuntu/myems-complete/others

# 1. 发布前记录最新日志
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml logs -t --since=10m modbus_tcp

# 2. 拉取并恢复采集服务
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml pull modbus_tcp
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml up -d --no-build modbus_tcp

# 3. 发布后检查最新日志
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml logs -t --since=5m modbus_tcp

# 4. 查看容器状态
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml ps modbus_tcp
```

### 12.7 场景六：离线镜像包交付

适用：生产机下载外网镜像缓慢、发布窗口有限、低内存生产环境。

在网络较好的中转机或验证机执行：

优先使用仓库内脚本自动打包当前发布范围：

```bash
cd /home/ubuntu/myems-complete
bash scripts/package-release-offline-images.sh \
  v2026.05.08-prod.1 \
  ghcr.io/tian0605/myems \
  /home/ubuntu/offline-packages \
  emqx/emqx:latest
```

说明：

1. 该脚本会拉取当前发布范围镜像：`web`、`admin`、`api`、`aggregation`、`cleaning`、`normalization`。
2. 该脚本会同时生成离线 tar 包、manifest 和 `docker-images.env`。
3. 若本次发布不涉及 MQTT 或采集链路，仍然不要顺带打包或重启 `emqx`、`myems_mqtt`、`modbus_tcp`。

若需要手工打包，仍可执行以下命令：

```bash
docker login ghcr.io -u <github-user>

docker pull ghcr.io/tian0605/myems/web:v6.3.6-prod
docker pull ghcr.io/tian0605/myems/admin:v6.3.6-prod
docker pull ghcr.io/tian0605/myems/api:v6.3.6-prod
docker pull ghcr.io/tian0605/myems/aggregation:v6.3.6-prod
docker pull ghcr.io/tian0605/myems/cleaning:v6.3.6-prod
docker pull ghcr.io/tian0605/myems/normalization:v6.3.6-prod
docker pull ghcr.io/tian0605/myems/modbus-tcp:v6.3.6-prod
docker pull ghcr.io/tian0605/myems/mqtt:v6.3.6-prod
docker pull emqx/emqx:5.8.6

docker save -o myems-v6.3.6-prod-images.tar \
  ghcr.io/tian0605/myems/web:v6.3.6-prod \
  ghcr.io/tian0605/myems/admin:v6.3.6-prod \
  ghcr.io/tian0605/myems/api:v6.3.6-prod \
  ghcr.io/tian0605/myems/aggregation:v6.3.6-prod \
  ghcr.io/tian0605/myems/cleaning:v6.3.6-prod \
  ghcr.io/tian0605/myems/normalization:v6.3.6-prod \
  ghcr.io/tian0605/myems/modbus-tcp:v6.3.6-prod \
  ghcr.io/tian0605/myems/mqtt:v6.3.6-prod \
  emqx/emqx:5.8.6

gzip -1 myems-v6.3.6-prod-images.tar
```

在生产机执行：

```bash
cd /home/ubuntu
gunzip -f myems-v6.3.6-prod-images.tar.gz
docker load -i myems-v6.3.6-prod-images.tar

cd /home/ubuntu/myems-complete/others
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml up -d --no-build web
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml up -d --no-build admin
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml up -d --no-build api
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml up -d --no-build cleaning normalization aggregation
```

### 12.7.1 当前版本推荐上线命令模板

适用：本次版本以当前已验收提交发布，release tag 假定为 `v2026.05.08-prod.1`。

生产机上线前准备：

1. 从 GitHub Actions 下载 `prebuild-metadata-v2026.05.08-prod.1`。
2. 将其中的 `docker-images.env` 上传到生产机：`/home/ubuntu/myems-complete/others/docker-images.env`。
3. 确认生产机可访问 GHCR，或提前准备离线镜像包。

若生产机可在线拉取，执行：

```bash
cd /home/ubuntu/myems-complete/others

# 1. 先升级前端
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml pull web admin
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml up -d --no-build web admin
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml ps web admin
curl -I --max-time 15 http://127.0.0.1/
curl -I --max-time 15 http://127.0.0.1:8001/

# 2. 再升级 API
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml pull api
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml up -d --no-build api
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml ps api
curl -i --max-time 15 http://127.0.0.1:8000/version | sed -n '1,20p'

# 3. 最后升级后台计算服务
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml pull cleaning normalization aggregation
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml up -d --no-build cleaning normalization aggregation
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml ps cleaning normalization aggregation
```

若本次不涉及 MQTT 或采集链路：

1. 不要重启 `emqx`。
2. 不要重启 `myems_mqtt`。
3. 不要重启 `modbus_tcp`。

若需要补发离线包到生产机，执行：

```bash
cd /home/ubuntu
docker load -i myems-v2026.05.08-prod.1-offline-images.tar

cd /home/ubuntu/myems-complete/others
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml up -d --no-build web admin api cleaning normalization aggregation
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml ps
```

### 12.8 场景七：全栈紧急维护

适用：重大配置变更、全栈重建、紧急恢复。

```bash
cd /home/ubuntu/myems-complete/others
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml pull
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml up -d --no-build
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml ps
```

警告：

1. 此方案风险最高。
2. 默认不作为常规发布方式。
3. 若涉及 `modbus_tcp`、`emqx` 或 `myems_mqtt`，必须执行停机前后数据时点核对。

### 12.9 场景八：审批后现场重建

适用：镜像产物损坏且离线包不可用，或必须在预发/应急环境现场验证 Dockerfile 变更。

```bash
cd /home/ubuntu/myems-complete/others
sudo docker compose -f docker-compose-on-linux.yml up -d --build <service-name>
sudo docker compose -f docker-compose-on-linux.yml ps
```

若本次已经审批允许在生产机现场构建，且生产机内存紧张，应优先按“先停高内存后台服务，再逐个重建目标服务”的方式执行，而不是直接全量 `up -d --build`。

适用范围：

1. 本次主要变更服务为 `api`、`admin`、`web`。
2. 不涉及 `modbus_tcp`、`emqx`、`myems_mqtt`。
3. 已明确这是一次例外的现场构建发布。

推荐命令：

```bash
cd /home/ubuntu/myems-complete/others
chmod +x build-on-production-low-memory.sh
sudo ./build-on-production-low-memory.sh api admin web
```

脚本行为：

1. 先停止 `aggregation`、`cleaning`、`normalization` 以释放内存。
2. 逐个停止并重建 `api`、`admin`、`web`，避免并发 build 占满内存。
3. 每个服务启动后立即执行 `ps`、最近日志和 HTTP 检查。
4. 构建完成后恢复 `aggregation`、`cleaning`、`normalization`。
5. 默认拒绝操作 `modbus_tcp`、`emqx`、`myems_mqtt`。

限制：

1. 低内存生产环境默认禁止使用。
2. 必须经运维负责人审批。
3. 仅允许对受影响服务做最小范围重建。
4. 若本次涉及采集链路，不能直接复用该脚本，必须单独设计停机与恢复顺序。

## 13. 发布后验证命令清单

### 13.1 容器状态检查

```bash
cd /home/ubuntu/myems-complete/others
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml ps
```

### 13.2 关键日志检查

```bash
cd /home/ubuntu/myems-complete/others
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml logs -t --since=10m api
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml logs -t --since=10m web
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml logs -t --since=10m admin
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml logs -t --since=10m cleaning
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml logs -t --since=10m normalization
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml logs -t --since=10m aggregation
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml logs -t --since=10m emqx
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml logs -t --since=10m myems_mqtt
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml logs -t --since=10m modbus_tcp
```

### 13.3 前后台连通性检查

```bash
curl -I http://127.0.0.1/
curl -I http://127.0.0.1:8001/
curl -i http://127.0.0.1:8000/ | sed -n '1,20p'
```

### 13.4 API 登录验证

```bash
curl -sS -X PUT http://127.0.0.1:8000/users/login \
  -H 'Content-Type: application/json' \
  -d '{"data":{"account":"REPLACE_ADMIN_ACCOUNT","password":"REPLACE_ADMIN_PASSWORD"}}'
```

说明：

1. 生产账号密码不得写死在仓库中。
2. 应从密码库中读取后执行验证。

### 13.5 数据库可达性检查

```bash
mysql -h 127.0.0.1 -u root -p -e 'SELECT VERSION();'
```

### 13.6 采集恢复检查

```bash
cd /home/ubuntu/myems-complete/others
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml logs -t --since=5m modbus_tcp
```

检查目标：

1. 不应持续出现数据库连接拒绝。
2. 不应持续出现网关不可用异常。
3. 若仍出现 `Data Source Not Found`，需核查后台配置是否完整。

### 13.7 MQTT 链路恢复检查

```bash
cd /home/ubuntu/myems-complete/others
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml logs -t --since=5m emqx
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml logs -t --since=5m myems_mqtt
```

检查目标：

1. `emqx` 不应持续出现监听失败、认证失败或集群异常。
2. `myems_mqtt` 不应持续出现数据库连接失败、订阅失败或 Topic 解析异常。
3. 若 MQTT 数据未继续入库，需核查 Broker 连通性、数据源配置、Topic 映射和 `.env` 中的连接参数。

## 14. 回滚操作模板

### 14.1 回滚触发条件

1. API 无法启动或无法登录。
2. Web 或 Admin 无法访问。
3. `modbus_tcp` 无法恢复采集。
4. `emqx` 或 `myems_mqtt` 无法恢复，导致 MQTT 数据持续无法入库。
5. 外部接口恢复失败且无法在窗口内修复。
6. 发布窗口超时，继续操作风险高于回滚风险。

### 14.2 应用回滚模板

前提：

必须已保存上一个可用版本代码或镜像版本。

模板：

```bash
# 1. 切换到上一版本代码或镜像标签
cd /home/ubuntu/myems-complete
# 按实际版本管理方式执行，例如 git checkout <last-good-commit>

# 2. 回到运行目录重新部署
cd /home/ubuntu/myems-complete/others
sudo docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml pull
sudo docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml up -d --no-build

# 3. 验证恢复情况
sudo docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml ps
```

若生产环境采用预构建镜像发布，回滚应优先切回上一个镜像标签并执行：

```bash
cd /home/ubuntu/myems-complete/others
sudo docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml pull
sudo docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml up -d --no-build
sudo docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml ps
```

### 14.3 配置回滚模板

适用：

1. `.env` 配置变更
2. `nginx.conf` 变更
3. Dockerfile 或依赖变更
4. EMQX 或 `myems_mqtt` 配置变更

模板：

```bash
# 1. 恢复配置备份
# 2. 使用已恢复的配置重新部署受影响服务
cd /home/ubuntu/myems-complete/others
sudo docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml pull api admin web cleaning normalization aggregation emqx myems_mqtt modbus_tcp
sudo docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml up -d --no-build api admin web cleaning normalization aggregation emqx myems_mqtt modbus_tcp

# 3. 验证服务状态
sudo docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml ps
```

### 14.4 数据库高风险变更回退说明

原则：

1. 数据库结构变更必须单独备份。
2. 若无验证通过的回退脚本，不得在常规发布窗口中直接执行高风险结构变更。
3. 数据库回退必须由数据库负责人确认后执行。

## 15. 数据对账模板

### 15.1 发布后数据对账表

| 项目 | 内容 |
| --- | --- |
| 发布编号 | |
| 发布日期 | |
| 发布开始时间 | |
| 发布结束时间 | |
| 停机开始时间 | |
| 停机结束时间 | |
| 影响服务 | |
| 是否涉及采集链路 | 是 / 否 |
| 是否涉及 MQTT 链路 | 是 / 否 |
| 是否涉及第三方接口 | 是 / 否 |
| 是否支持补传 | 是 / 否 |
| 对账责任人 | |
| 复核人 | |

### 15.2 关键点位对账表

| 数据源/接口 | 关键点位 | 停机前最新时间 | 停机后最新时间 | 停机前数值 | 停机后数值 | 是否连续 | 是否补传 | 处理结论 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |  |  |  |
|  |  |  |  |  |  |  |  |  |

### 15.3 报表与聚合结果对账表

| 检查项 | 检查时间 | 结果 | 说明 |
| --- | --- | --- | --- |
| 前台首页可访问 |  | 正常 / 异常 |  |
| 后台首页可访问 |  | 正常 / 异常 |  |
| API 登录正常 |  | 正常 / 异常 |  |
| 采集时间继续推进 |  | 正常 / 异常 |  |
| MQTT Dashboard 正常 |  | 正常 / 异常 |  |
| MQTT 入库日志正常 |  | 正常 / 异常 |  |
| 归一化结果继续更新 |  | 正常 / 异常 |  |
| 聚合结果继续更新 |  | 正常 / 异常 |  |
| 关键报表无明显跳变 |  | 正常 / 异常 |  |

## 16. 常见告警与处理建议

### 16.1 `modbus_tcp` 停机风险告警

告警：

`modbus_tcp` 停机期间，实时轮询数据默认不可自动补采。若设备侧没有历史回读能力，则该时段存在永久性数据盲区风险。

处理建议：

1. 缩短停机时间。
2. 记录停机前后关键时间点。
3. 恢复后立即核对采集连续性。

### 16.2 API 发布风险告警

告警：

API 发布会直接影响前后台可用性和第三方接口调用，应避免与采集链路高风险变更同时执行。

处理建议：

1. API 发布与采集发布分开执行。
2. 必须提前通知接口调用方。

### 16.3 MQTT 链路发布风险告警

告警：

`emqx` 或 `myems_mqtt` 发布会直接影响 MQTT 入站与入库。若设备端不支持补发或 Broker 未配置保留/持久会话，停机窗口内可能出现数据丢失。

处理建议：

1. 优先采用预构建镜像，缩短停机时间。
2. 发布前记录关键 Topic 的最新上送时间和 `myems_mqtt` 最近日志。
3. 恢复后立即验证 Broker 监听、订阅成功和数据库落点是否继续推进。

### 16.4 配置变更风险告警

告警：

当前 Compose 结构中，部分配置在构建阶段生效。仅重启容器可能无法应用新配置。

处理建议：

1. 先确认是否必须 rebuild。
2. 尽量缩小重建范围。

## 17. 当前环境执行要求

1. 任何常规发布默认在工作时间后执行。
2. 不涉及采集链路的发布，禁止顺带重启 `modbus_tcp`。
3. 不涉及 MQTT 链路的发布，禁止顺带重启 `emqx` 和 `myems_mqtt`。
4. 涉及外部计量数据接口的变更，必须先定义停机策略和恢复策略，再批准发布。
5. 任何包含数据库结构变更的发布，必须有单独备份和回退方案。
6. 本文档中的命令模板默认由具备 `sudo docker` 权限的运维人员执行。

## 18. 后续建议

建议在正式制度中再补充以下内容：

1. 发布审批人和值班人名单。
2. 发布通知模板。
3. 发布失败升级路径。
4. 数据对账结果归档规范。

## 19. 历史版本发布指引（2026-05产品碳足迹）

本节保留2026-05产品碳足迹版本从本地仓库发布到 GitHub、再由 GitHub Actions 进入生产升级的历史指引，不是集社云接入的发布步骤。

### 19.1 当前版本源码基线

当前本地已确认提交：

`d5203d4 Add product carbon footprint module and upgrade guide`

建议：

1. 当前工作分支如仍为功能分支，先推送对应发布分支；若已在 `main`，则直接推送 `main`。
2. 再创建 release tag `v2026.05.14-prod.1`。
3. 由 Actions 构建镜像并生成 metadata artifact。
4. 本次产品碳足迹专项升级细节，参见 `docs/PRODUCT-CARBON-FOOTPRINT-PRODUCTION-UPGRADE-CN.md`。

### 19.2 当前版本推荐发布分类

本次版本建议按以下发布分级处理：

1. 前端：A 类。
2. API：B 类。
3. 后台管理：B 类。
4. 后台计算服务：C 类。
5. 采集链路：本次默认不涉及，不应重启 `modbus_tcp`、`emqx`、`myems_mqtt`。

### 19.3 当前版本推荐生产升级顺序

1. 升级 `web`。
2. 升级 `admin`。
3. 升级 `api`。
4. 升级 `cleaning`、`normalization`、`aggregation`。
5. 执行数据库脚本 `database/upgrade/upgrade6.3.8.sql`。
6. 完成产品碳足迹、菜单权限与 PDF 导出的专项回归。

### 19.4 当前版本重点验收点

上线后至少回归以下页面：

1. 后台管理端：空间产品绑定、菜单模板与产品碳足迹菜单配置正常。
2. 用户端产品碳足迹核算页面：仅可看到当前空间已绑定产品的数据。
3. 年度核算列表：可正确展示“系统边界”、总碳足迹和数据状态。
4. 生命周期五阶段：活动新增、编辑、删除及阶段占比计算正常。
5. 公式校验：排放量为 `活动水平 * 因子`，碳足迹贡献为 `排放量 / 产量`，总碳足迹为全部活动贡献求和。
6. 报告导出：点击“生成报告”后可下载 PDF，且 PDF 不包含界面操作按钮。
7. 数据库校验：`tbl_versions` 已更新至 `6.3.8`，菜单 `1300/1301/1302/1303` 已正确写入。

## 20. 集社云电表接入版本上线说明（2026-10）

### 20.1 版本基线、范围与上线闸门

- 源码基线：`34136ad`（`feat(jishiyun): deliver audited nine-meter acquisition pilot`），分支 `feature/jishiyun-api-integration-v2`。这是已推送的功能分支，不代表已经合并主分支、生成生产镜像或发布生产。发布负责人另行批准不可变release tag/镜像digest，并核对各镜像对应的提交。
- 仅接入 `/NH2/AmmeterBasicInfo` 电表档案、`/NH2/ElectricQuantity` 电量接口。独立worker调用供方，API只提供管理员控制、查询及共享处理逻辑，不在API请求中直接调用供方。
- 已验收：9台有效电表，常规采集、历史补数、映射与任务、源级原始结果、分阶段处理状态、UTC+8时间、执行起止时间；真实跨日24小时数据及测试费用/碳排、直接输入空间/设备分类、报表导出。最终代码回归408项通过，不能据此承诺70台容量或连续可用性。
- 版本详情见[版本计划](集社云接口接入版本计划.md)、[人工验收脚本](集社云人工验收测试脚本.md)、[worker说明](../myems-jishiyun/README.md)。

| 发布前置项 | 要求与当前边界 |
| --- | --- |
| 本次放行规模 | 现有证据仅覆盖9台；70多台全量前完成容量、并发、长补数及数据库负载验证，不直接按设备数量外推 |
| 连续运行 | 补齐的24小时数据不等于无中断观察；完成连续24小时采集/业务处理监控和停机恢复演练 |
| 传输安全 | 真实供方试点是HTTP明文；本机试点授权不继承为生产授权，默认禁止生产开启HTTP测试豁免 |
| 数据库 | 增量迁移、跨库事务、备份恢复及最小权限在生产等价预发环境验证后签收 |
| 正式费用/碳排 | 正式费率、碳因子、有效期、小时内切价和账单舍入政策必须确认；当前自动结算开关仅用于测试，不能承诺正式自动结算 |
| 原始修订 | 批准式CLI及隔离副本并发/回滚已验证；真实供方修订事件仍待复验，不自动批准历史重算 |
| 派生统计 | 只签收已批准的直接输入空间/设备分类；输出、分项、任意层级、虚拟/组合/车间/商铺/租户等不能默认覆盖 |

本次属于B/C/D/E混合发布：涉及API、管理端、聚合writer隔离、新采集服务及数据库扩展。不能套用“仅前端发布”顺序。前置未满足时只允许经批准的受限试点，不标记为生产全量验收。

### 20.2 服务、镜像与生产交付要求

| 服务/产物 | 本次要求 |
| --- | --- |
| `api` | 更新镜像；构建上下文必须是仓库根目录，Dockerfile为 `myems-api/Dockerfile`，镜像内安装本仓库 `myems_jishiyun` 共享包 |
| `admin` | 更新工作台、语言资源及Nginx配置；入口HTML no-cache，语言脚本版本随发布更新；已打开页面需刷新，必要时Ctrl+F5 |
| `aggregation` | 在启用新业务writer前升级；包含电表费用/碳排与6个空间/设备输入分类循环的独占writer过滤，不能继续运行不识别本次归属标记的旧镜像 |
| `jishiyun_worker` | 新增独立镜像/服务，无需暴露HTTP端口；以非root用户运行，保留只读根目录、可写 `/tmp`、降权及健康检查配置 |
| 其他服务 | 本次不要求为了接入而重启 `web`、`emqx`、`myems_mqtt`、`modbus_tcp`、`cleaning`、`normalization`；已有链路保持原配置 |

当前 [Actions预构建工作流](../.github/workflows/prebuild-images.yml) **没有包含worker镜像**；现有 [Linux纯镜像模板](../others/docker-compose-on-linux.image.yml) 也没有worker服务。不能只下载既有metadata就认为本次交付齐全。

在可信构建机补交worker镜像及其digest/离线包，API、Admin、aggregation也必须来自本次批准基线。以下为构建机Linux示意，镜像名和标签由发布负责人替换，**不是生产现场构建命令**：

```bash
cd /path/to/approved-checkout
docker build -f myems-api/Dockerfile -t myems/api:<approved-release> .
docker build -f myems-jishiyun/Dockerfile.worker -t myems/jishiyun-worker:<approved-release> myems-jishiyun
```

生产继续采用 `pull` 或 `docker load` 后 `up -d --no-build`。记录镜像清单、架构、digest及回退兼容镜像，不使用 `latest`。供方Key不得作为build arg、镜像层或源码打包进入镜像。

### 20.3 worker生产环境变量

配置来源为 [example.env](../myems-jishiyun/example.env) 与 [worker入口](../myems-jishiyun/myems_jishiyun/worker.py)。将模板复制到目标环境的 `myems-jishiyun/.env`，由受限权限部署文件或受控秘密注入；**不可复制本机试点 `.env` / `state`**。文件不得入库、进入镜像或发到公共工单。以下只给变量名和约束，不包含实际凭据。

| 变量 | 必填/默认值 | 生产配置要求 |
| --- | --- | --- |
| `JISHIYUN_API_KEY` | `run`及额度检查必填，无默认 | 供方分配的正式凭据；非空可见ASCII，不含空格/换行；仅worker持有，不发到浏览器或管理端配置响应 |
| `JISHIYUN_BASE_URL` | `run`必填，无默认 | 经确认、证书有效的HTTPS URL，路径必须为 `/WebApi`；不包含接口子路径、用户名密码、query或fragment；禁止用认证网站地址代替实际数据接口 |
| `JISHIYUN_ALLOW_HTTP_TEST` | 可选，`false` | 只接受小写 `true` / `false`。生产保持 `false`；`true` 仅允许 `http://nhapi.yunjichaobiao.com/WebApi` 的80端口测试豁免，不是通用HTTP开关 |
| `JISHIYUN_DATA_SOURCE_ID` | 必填，无默认 | 目标系统已创建且属于本次接入的正整数数据源ID；任务、映射、运行、额度和业务审计均需按该源核对，不照抄本机源4 |
| `JISHIYUN_DB_HOST` | 必填，无默认 | 从worker容器实际可达的MySQL地址；宿主机MySQL应配置可达内网地址/经配置的host gateway，容器内 `127.0.0.1` 不是宿主机 |
| `JISHIYUN_DB_PORT` | 可选，`3306` | 整数1～65535；数据库不暴露公网 |
| `JISHIYUN_DB_USER` | 必填，无默认 | 专用最小权限运行账号，不使用试点root；权限需覆盖实际启用阶段及跨schema事务，见20.4 |
| `JISHIYUN_DB_PASSWORD` | 必填，无默认 | 正式秘密；不能留空、写入本文或命令行历史；轮换后需重建worker容器并验证，不只执行restart |

即使不启用采集定时器，`run` 仍会检查URL/Key并消费已排队任务；纯数据库预览类CLI不需要供方URL/Key。worker当前固定使用 `myems_system_db` 等代码中的标准schema名称，**没有** `JISHIYUN_DB_DATABASE` 可供改名；不能将不同schema名称仅通过既有应用变量映射后假定兼容。

**HTTP生产阻塞：** 供方未提供可验证HTTPS时，不能填一个推测的HTTPS地址，也不能关闭证书校验。本版本没有独立的生产HTTP授权机制。应优先与供方落实加密数据入口；若仍需例外，必须另行安全评审、生产风险签署及受控方案，不把 `ALLOW_HTTP_TEST=true` 作为常规上线步骤。认证系统自身使用HTTPS，不等于供方电量接口加密。

### 20.4 既有应用变量、数据库与权限

- API继续使用 `myems-api/.env`，aggregation使用 `myems-aggregation/.env`，不因worker新增而改用 `JISHIYUN_DB_*`。按各自 `config.py` 复核既有数据库连接。
- 与本链路直接相关的配置族为 `MYEMS_SYSTEM_DB_*`、`MYEMS_HISTORICAL_DB_*`、`MYEMS_ENERGY_DB_*`、`MYEMS_BILLING_DB_*`、`MYEMS_CARBON_DB_*`。每族后缀是 `HOST`、`PORT`、`DATABASE`、`USER`、`PASSWORD`；原有其他业务库配置继续保留。不要依赖代码开发默认的root、空密码或容器内loopback。
- API的处理状态查询通过共享队列连接读取跨库原始/业务证明，系统库连接账号也需相应跨库读权限；API执行任务、映射/档案操作还需批准范围内写权限。只给单库权限可能导致页面或操作失败。
- API Redis配置沿用 `IS_REDIS_ENABLED`、`REDIS_HOST`、`REDIS_PORT`、`REDIS_PASSWORD`、`REDIS_DB`，不新增worker Redis。暂存电表及直接关联空间的相关报表已绕过Redis，其他对象仍沿用缓存；不通过flush Redis解决本次验收。
- `LOCAL_*`、本机容器名、actor 1、source 4、space 2、equipment 1、product 2及测试成本中心/类别2均为本机验收配置，不是生产默认值。

**数据库前置：**

1. 系统库、历史库、能耗库、计费库、碳排库必须在同一个MySQL实例，参与本链路写入/审计的表使用InnoDB。worker使用单连接跨schema事务，不支持用五台独立数据库服务器替代。
2. DBA备份涉及的配置、原始数据、业务数据、任务/审计表，并完成可恢复性检查；迁移账号与运行账号分离。运行账号不应拥有建库、删库、任意DDL权限；根据采集、初始化、业务写入、纠正等批准功能逐表授予所需 `SELECT/INSERT/UPDATE/DELETE`，在预发验证，不在此给全库root式授权。
3. 在已安装基础库的环境，仅执行 [集社云增量脚本](../database/install/myems_jishiyun.sql)，**不要重跑全量安装脚本**。脚本创建10张系统扩展表及1张历史原始表，其中必须包含 `tbl_jishiyun_business_jobs`；不改系统版本号、不删除已有业务数据。
4. 脚本使用 `CREATE TABLE IF NOT EXISTS`，重复执行不等于已核对旧表结构；核对已有扩展表定义、索引和InnoDB，尤其升级早期试点环境。DDL不能依赖事务整体回滚，失败时由DBA检查实际已创建部分。
5. 迁移后先只读核对，再启用写入；执行开始/完成时间和任务摘要复用已有字段，不需要另造一份“已处理”布尔字段迁移。

Linux生产机迁移示例，`mysql --login-path` 所用账号由DBA在本机安全配置，不能把密码直接写在命令中：

```bash
cd /home/ubuntu/myems-complete
mysql --login-path=myems-release < database/install/myems_jishiyun.sql
```

### 20.5 数据源、映射、任务参数与独占writer

以下是数据库持久配置，不是环境变量；通过管理员工作台审核配置，不新增臆造的 `JISHIYUN_SCHEDULE_*` 变量。

| 配置 | 要求 |
| --- | --- |
| 数据源/任务类型 | 新建目标数据源；档案获取/初始化使用 `meter_initialize`、常规电量 `energy_collect`、历史补数 `energy_backfill`，任务/映射先保持暂停；DDL中的 `archive_sync` 不是当前工作台档案获取任务类型 |
| 供方时区 | `provider_utc_offset_minutes=480`；数据库存UTC，工作台固定UTC+8，提交时间必须带时区，不用宿主机 `TZ` 替代供方时区 |
| 请求编码 | 任务参数 `encoding` 按供方验证结果配置 `json` 或 `form`，不混用；档案和电量按各自实际契约验收 |
| 计划分钟 | `schedule_minute`，0～59，表示供方本地每小时触发分钟 |
| 数据延迟 | `delay_minutes`，0～65535；应留足20～30分钟延迟并按实测保守配置，例如40分钟 |
| 回看窗口 | `lookback_hours`，1～24；建议试点从重叠3小时开始评估，用幂等抵御重复，不设为无限历史回补 |
| 映射范围 | 核对唯一表地址、外部编号、电表/点位及生效时间；档案初始化不自动启用采集，审核后才批量确认启用，无效设备继续暂停 |
| 点位归属 | 专用 `JISHIYUN_REGISTER`，单位kWh、ratio=1、offset=0，一表一点独占；不能改成旧 `ENERGY_VALUE` 后让旧normalization重复计算 |

调度窗口计算为：最近到期tick减 `delay_minutes` 后向下取整到整点，再回看 `lookback_hours`。例如北京时间13:40、分钟40、延迟40、回看3小时，对应 `[10:00,13:00)`，不是 `[10:40,13:40)`。调度仅提交最近tick的窗口；停机超出回看范围的历史缺口需另建补数任务。

原始口径为15分钟时刻累计正向有功（`ZYGDNSZ`，kWh），不重复乘倍率；已确认的午夜23:59报文按次日00:00口径规范。每小时需起点、+15、+30、+45、终点五点有效且身份/修订一致，小时量 `R(end)-R(start)`；缺失不填0、不插值。

**防双writer：** aggregation升级后会排除暂存点关联电表的旧计费/碳排writer；已批准直接输入维度使用精确 `JISHIYUN_PILOT_SOURCE:<source_id>` 归属标记，由独立worker拥有writer。不得随意删除标记、为同一对象启动旧循环、手工写业务目标或通过改点类型绕过保护。同一电表不能同时直接计入空间、又经设备重复计入该空间。源锁只约束遵循同一协议的writer，不保证任意外部SQL写入安全。

### 20.6 生产纯镜像worker配置与启动开关

[通用worker模板](../others/docker-compose-jishiyun-worker.yml) 使用 `build`，默认命令只有 `run --health-file ...`，**默认不开启** `--schedule` 和 `--business-schedule`。生产不直接把该开发构建模板叠到纯镜像模板并期待自动调度。

在生产 `others` 目录由运维维护 `docker-compose-jishiyun.production.yml`，示例如下（该文件为目标机部署配置，不是仓库已交付的现成文件）。与主纯镜像Compose组合使用，保持相同项目名；如既有项目使用 `-p`，下文所有命令也必须带同一个 `-p`：

```yaml
services:
  jishiyun_worker:
    image: ${MYEMS_JISHIYUN_IMAGE:?approved worker image is required}
    env_file:
      - ../myems-jishiyun/.env
    command: ["run", "--health-file", "/tmp/jishiyun-worker-health.json"]
    restart: on-failure
    read_only: true
    tmpfs:
      - /tmp:rw,noexec,nosuid,size=1m,mode=1777
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
```

`MYEMS_JISHIYUN_IMAGE` 是**上述部署示例引入的Compose插值变量**，不是worker读取的环境变量；运维需把批准的不可变镜像引用追加到目标机 `others/docker-images.env`。现有Actions不会自动生成该项。已有 `MYEMS_API_IMAGE`、`MYEMS_ADMIN_IMAGE`、`MYEMS_AGGREGATION_IMAGE` 也必须更新到本次批准镜像。普通Docker Compose的 `env_file` 是容器环境注入，不等于应用支持自动读取Docker secret文件。

| 启动参数 | 含义及启用要求 |
| --- | --- |
| `run --health-file /tmp/jishiyun-worker-health.json` | 消费已排队档案/电量任务，不创建每小时计划；不是“不写数据库”的dry-run |
| 增加 `--schedule` | 提交当前源下已启用 `energy_collect` 的最近到期窗口，不自动定时拉档案，也不自动补齐全部停机历史 |
| 增加 `--business-schedule --business-actor <正式操作人ID> --confirm-timestamp-kwh` | 开启持久业务队列，首次写入累计业务表及小时能耗；操作人ID用于审计而非鉴权，启动权限由运维控制 |
| `--pilot-test-settlement` | 仅获批测试参数的费用/碳排及后续直接输入维度自动处理；**正式生产默认禁止加入** |
| `--once` | 最多领取一个采集任务并执行本轮业务逻辑后退出，属于受控执行，不作为长驻健康进程 |

按审批阶段修改示例中的 `command`，不能把全部开关一次复制启用。仅采集定时器的命令是：

```yaml
command: ["run", "--schedule", "--health-file", "/tmp/jishiyun-worker-health.json"]
```

采集及小时能耗经受控预览签收后，再改为下列命令；`<正式操作人ID>` 必须替换为目标环境批准的正整数ID，不能原样使用：

```yaml
command: ["run", "--schedule", "--business-schedule", "--business-actor", "<正式操作人ID>", "--confirm-timestamp-kwh", "--health-file", "/tmp/jishiyun-worker-health.json"]
```

**重要行为边界：** 开启业务调度会发现当前源下尚未登记业务作业的历史 `done` 电量运行（每轮最多100条），不只处理开启后新采集的运行；放行前审核历史范围。关闭业务调度不会撤销已经提交的业务数据。已 `done` 作业不会因为后加下游开关而重新扫描或自动批准原始修订。

正式费率的受控首次结算可用 `business-settle --run-id <运行ID> --actor <操作人ID>` 先预览，经审核加 `--apply`；**不加 `--test-only`** 才走非 `PILOT TEST` 参数校验。当前要求完整小时唯一人民币分时费率、整个小时单一价格和碳因子，六位小数 `ROUND_HALF_UP`，小时内切价/重叠/缺档会阻塞，不是正式两位账单政策。尚无与测试开关对等的正式自动结算调度选项，若要求生产自动费用/碳排全链路，应先补齐并验收，不能借测试开关代替。

当前直接输入空间/设备分类亦是明确配置后的受控试点能力，预览/执行见验收脚本；不会因为开启小时能耗调度就自动完成全部派生统计。能耗提交、费用/碳排提交、维度提交是后继可恢复事务，后阶段阻塞时前阶段可能已成功，不宣称整个链路一次回滚。

### 20.7 分阶段上线SOP

以下为Linux生产命令，工作目录是 `others`，不是本机 `myems-jishiyun-local`。命令中的部署文件、镜像及凭据均须已按前述准备；此处不执行真实环境变更。

1. **审批与备份。** 明确仅采集/小时能耗/受控结算的放行边界，登记设备及历史窗口、传输风险、操作人、停机目标和回退镜像；备份数据库和部署配置。
2. **迁移与预检。** 保持新任务/映射暂停、worker未运行；执行20.4增量迁移，核对11张扩展表及跨schema事务/权限；确认供方可达、正式凭据、时区和不超过10次/分钟限制。同Key其他调用方必须统一控流，不能只依赖本worker的共享额度。
3. **交付镜像。** 在生产拉取/加载批准的API、Admin、aggregation、worker镜像。组合配置用 `config --quiet` 验证，不把展开后含秘密的 `docker compose config` 输出到工单或公共日志。

```bash
cd /home/ubuntu/myems-complete/others
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml -f docker-compose-jishiyun.production.yml config --quiet
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml -f docker-compose-jishiyun.production.yml pull api admin aggregation jishiyun_worker
```

离线交付时，用核对过digest的 `docker load` 替代 `pull`，不切回现场build。

4. **先升级writer隔离，再发布控制界面。** 在启用任何新业务写入前升级aggregation；随后更新API和Admin。不要用第8节通用顺序替代本次隔离前置，不顺带重启其他采集服务。

```bash
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml -f docker-compose-jishiyun.production.yml up -d --no-deps --no-build aggregation
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml -f docker-compose-jishiyun.production.yml up -d --no-deps --no-build api admin
```

5. **初始化与原始验收。** 工作台建立当前源的任务，批准启用 `meter_initialize` 任务后排队一次档案获取；用默认无调度worker消费，审核预览/配置，批准后初始化映射，再启用已批准设备及手动验收所需电量任务。通过工作台发起小窗口手动采集，验证地址、单位、时区、延迟、共享边界、缺失告警及重复提交幂等。此阶段不启用调度参数；启动前确认队列内没有未经批准的历史作业。

```bash
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml -f docker-compose-jishiyun.production.yml up -d --no-deps --no-build jishiyun_worker
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml -f docker-compose-jishiyun.production.yml ps api admin aggregation jishiyun_worker
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml -f docker-compose-jishiyun.production.yml exec -T jishiyun_worker python health.py
```

6. **受控业务验收。** 选成功整点1～24小时运行，用 `business-hour --run-id <运行ID> --actor <操作人ID> --confirm-timestamp-kwh` 先预览，签收再加 `--apply`。以独立SQL/报表核对五点、小时差值、唯一目标行及审计proof；费用/碳排、维度分别按20.6审批，不能只看采集done。
7. **逐项开调度。** 先在目标机Compose命令增加 `--schedule` 并重建worker；按批准范围启用采集任务。小时能耗验收通过、历史运行范围已审核后，再加入业务调度三参数并重建。仍用步骤5的 `up -d --no-deps --no-build jishiyun_worker`；环境变量或命令变更不能只用 `docker compose restart`。
8. **补数与观察。** 对停机/初始化缺口显式补数，一批不超过31天、按最多一天拆子任务；业务验收使用整点窗口。重复同一提交复用request key和相同参数，变更窗口用新key；不盲目重发。完成连续观察及20.8对账后，才决定扩大设备范围。

**禁止把测试配置迁入生产：** 本机测试电价2.3元/kWh、碳因子1.5kgCO₂e/kWh仅为隔离验收参数，已授权有效期截至北京时间 **2026-10-19 00:00**，到期不自动续期。不能复制本机9台配置脚本、来源ID、登录文件、测试SQL副本或 `compose.provider.yml` / `compose.schedule.yml` 到生产，也不能运行本机创建/删除临时schema的修订演练脚本作为生产迁移。

### 20.8 上线验收、日常监控与故障判定

| 核对面 | 必须检查 |
| --- | --- |
| 容器健康 | `health.py` 验证本地循环心跳在240秒内；Docker healthy不代表供方成功、数据完整或全部业务已完成 |
| 任务与执行 | 核对实际开始/完成时间、数据窗口、最近状态、上次成功时间；失败也可有终止时间，不能把“有完成时间”当成功；所有工作台时间为UTC+8 |
| 原始结果 | 当前源“采集接口执行结果”覆盖常规与补数，分页默认50/最多200、按原始编号倒序；同键只存当前行，不是每次HTTP响应的流水副本 |
| 质量与处理 | 有效/无效/冲突是原始质量，不等于下游完成；分别核对小时能耗、费用/碳排、空间/设备及只读处理备注 |
| 处理状态 | 已处理、部分已处理、待处理、已排队、处理中、受阻、失败、需复核/重算、未启用；以当前修订、有效审计及目标业务唯一行匹配判定，不以job done或last_run推断 |
| 原始运行编号 | 首次运行是首次插入原始行的运行；末次写入/复核运行即使值没变也可能更新，不代表下游完成；revision才反映身份/值/质量变化 |
| 幂等与小时完整 | 相同窗口重放不增加重复业务行；整点边界参与前后小时，缺一个结束边界可受阻而非补零；重叠窗口的总量不能相加 |
| 下游报表 | 独立SQL/报表/XLSX三方核对电表小时量及批准范围的费用/碳排、直接输入分类；空间与设备是不同统计口径，不能重复相加 |
| 运维容量 | 观察供方额度/冷却、排队积压、失败/partial/missing、blocked原因、数据库锁等待/事务耗时、磁盘和日志增长，扩大设备前保存基线 |

契约错误仅在失败及审计成功持久化后允许worker继续；数据库/系统/失败持久化异常仍退出，不能当作“已跳过坏数据”。遇到退出、心跳过期或调度积压需显式告警，不用无限重启掩盖。

数据修订或目标被删改时按备注处理，使用 `business-correct` 单独预览，复核精确旧/新差值与审批哈希后才执行 `--expected-hash ... --apply`。无工作台重算审批按钮；不得更新状态表假造done或手工覆盖原始审计。没有统一的停机缺口自动巡检/完整健康总览，下次计划时间也不能视为已交付功能，当前需运维分视图检查。

归档只保存脱敏运行编号、窗口、计数、耗时、gate code和对账结论；不保存供方Key、完整含认证请求、密码、展开后的环境配置或未经处理的供方报文。

### 20.9 暂停、故障恢复与回退

1. **先控写入面。** 停止新采集/补数提交，按工作台保护规则暂停任务/映射。去掉 `--schedule` 只是不再生成计划，已有排队任务仍会被 `run` 消费；若要停业务还要关闭 `--business-schedule`。需要完全停止该链路时，停止独立worker，不影响其他采集容器。
2. **记录并平稳停止。** 保存当前运行/业务作业、租约、最后完整数据/业务小时、暂停时间及已有提交阶段；允许在途工作结束，必要时给足优雅退出时间。异常强制中断后不手工清租约/改done，恢复时按租约与审计proof接续并核对是否已提交。

```bash
cd /home/ubuntu/myems-complete/others
docker compose --env-file docker-images.env -f docker-compose-on-linux.image.yml -f docker-compose-jishiyun.production.yml stop -t 120 jishiyun_worker
```

3. **应用回退要保持隔离。** API/Admin/worker需兼容同一数据模型。仍存在 `JISHIYUN_REGISTER` 或归属标记数据时，不能把aggregation回到不识别隔离规则的旧版继续运行；优先停新worker、保留已升级隔离writer的版本，再评估回退范围。禁止通过删标记/改点类型让旧writer接管。
4. **保留证据。** 不删除原始记录、任务、映射、proof、纠正事件来“回到上线前”；扩展表通常保留。数据库恢复必须单独审批、停止相关writer并对涉及schema一致恢复，评估其他采集链路新增数据损失，不能只恢复某张小时表或只DROP新增表。
5. **恢复和补缺。** 先验证DB/权限、镜像、环境和隔离，再以受控命令恢复队列，核对重放无重复。按暂停前后时间列出超过回看范围的缺口，人工补数并逐阶段验收；不把容器重新healthy当作数据已全部追平。
