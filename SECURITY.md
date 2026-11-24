# 安全策略

Veridrop 的产品本质是「用户把 API key 交给我们检测中转站真伪」。
**所以安全不是我们勾的某个选项,而是产品本身**。

## 我们怎么处理 API key

这是用户最关心的事,代码可逐行验证:

| 我们做了什么 | 你在哪里能验证 |
|---|---|
| API key 只活在内存里的 `Job` 对象中,检测完成立即清空 | [`web/jobs.py`](web/jobs.py) 搜 `api_key` |
| key **永不**写入报告 JSON | [`web/jobs.py:_run`](web/jobs.py) 里只有 `mask_api_key()` 输出落盘 |
| key **永不**写日志 | grep print/log 语句,不存在带 `api_key` 字段的输出 |
| key **永不**写数据库 | 我们根本没有数据库 — 只有 JSON 文件,且都已脱敏 |
| 报告里的 key 显示为脱敏形式 `sk-y7xU••••••0h` | [`mask_api_key`](src/relay_detector/models.py) 函数 |
| `.env` 文件 gitignored | [`.gitignore`](.gitignore) |
| 生产部署在自有 VPS,不经任何第三方处理器 | 永不出售、共享、代理 key 给任何上游之外的厂商 |

上表说的是本仓库的自托管版本。**线上服务(veridrop.app / veridrop.org)有一处不同**:它用多进程
任务队列,排队和检测期间会把经过认证加密的 key 密文临时写入任务队列表,任务成功、失败或超时后
立即清空;明文同样不写报告、日志和数据库。站长主动开启持续探测时,专用 key 以密文保存到撤销为止。

不放心 SaaS,**clone 到自己机器跑**。本仓库公开的是检测引擎和自托管版本;线上服务在此之上
还有任务队列、排行榜等部分。

## 报告漏洞

如果你发现下面任何一种情况:

- 能从运行中的服务里提取 API key 的途径(内存 / 进程 / 日志)
- 能让 key 明文写到磁盘的代码路径,即使是临时的
- 把用户 key 发到非上游目标的代码路径
- 让假冒中转站被错判为真品的检测旁路
- 其他安全相关的问题

**请不要开公开 GitHub issue**。请改用以下方式之一:

- 邮件 / 标记 `[SECURITY]` 但不带敏感细节的 issue,要求私聊渠道;或
- 用 GitHub 自带的[私下漏洞披露](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
  功能(repo 主页 Security 标签)

我们目标 **72 小时内回复**。确认有效后:

- 修复
- 通知 veridrop.org 在线服务运营
- 在合理披露窗口(通常 14 天,严重的会更长)后,通过
  [GitHub Security Advisories](https://github.com/canarybyte/veridrop/security/advisories) 公开
- 给报告者署名(如希望匿名我们尊重)

## 范围内(In Scope)

- 本仓库的代码、配置、默认部署设置
- veridrop.org 在线服务
- API key 处理(内存 + 网络 TLS 期望)
- 任何后续管理后台的 auth / authz

## 范围外(Out of Scope)

- **上游中转站漏洞**:`some-relay.com` 自己有 bug 是它们的责任,Veridrop
  只负责检测它们
- **DoS / 限流**:我们就一台 VPS,被 DDoS 是常态运维问题不算漏洞
- **钓鱼网站**:网上可能有人开 `veridrop.io` / `veridrop.cn` 仿站偷 key,
  我们知道。**只信 `veridrop.org` 这一个域名**
- **自托管错配**:你启 verbose 日志 / 暴露 `web_data/` 是你自己的事,不是
  我们的漏洞

## 防御深度

我们设计时假定服务器本身可能被打穿,所以:

- key 明文不接触磁盘 — 自托管版本里 key 只在内存;线上服务的数据库里最多只有检测进行中的临时密文
- 报告写盘前已脱敏 — 不存在"原始未脱敏"的中间形态
- 代码里没有 verbose 模式会打印 key(故意没有)

如果你在代码里发现以上任何承诺有反例,**请报告**。

## 漏洞赏金

我们没有付费 bounty 计划。但有:

- 公开 credit
- commit co-author 署名(如你愿意)

AGPL 意味着 Veridrop 是社区项目,不是 VC 公司,抱歉没现金。

谢谢你读完。Veridrop 只在它真的可信的时候才有价值,而它持续可信的唯一办法
是有像你这样的人来不断检验它。
