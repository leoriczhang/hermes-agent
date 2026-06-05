# Hermes Agent — 团队定制版

> 基于 [hermes-agent](https://github.com/nousresearch/hermes-agent) 深度定制，接入 OpenViking 持久记忆 + SkillClaw 技能自进化 + DreamCycle 异步维护。

---

## 我做了什么

在开源 hermes-agent 基础上，增加了三个核心能力：

| 能力 | 说明 | 效果 |
|------|------|------|
| **OpenViking 记忆** | 替换原有的本地记忆，接入团队共享知识库 | 你和 agent 说过的事情，下次、下下次都还记得；团队知识互通 |
| **SkillClaw 技能进化** | 从大家的对话中自动提炼可复用的经验 | agent 犯过的错不会再犯，学会的技巧全组共享 |
| **DreamCycle 夜间维护** | 每天凌晨自动整理团队知识库 | 去重、归档过期信息、维护团队概况 |

### 具体改动清单

```
hermes-agent/
├── tools/
│   ├── viking_*.py          # [新增] OpenViking 7 个记忆工具
│   └── skills_hub_openviking_source.py  # [新增] 从 OpenViking 加载进化 skill
├── config.yaml              # [修改] memory provider 切到 openviking
├── env.template             # [新增] 一键配置模板
└── TEAM_GUIDE.md            # [新增] 本文件

服务器 10.37.243.72:
├── OpenViking/              # 记忆数据库（端口 1933）
├── SkillClaw/               # 技能进化引擎
│   ├── proxy (端口 30000)   # 对话录制代理
│   └── evolve_server        # LLM 进化消费者
└── DreamCycle/              # 异步维护 agent
```

---

## 系统架构

```
你的 Mac                              服务器 10.37.243.72
┌────────────┐                       ┌─────────────────────────────────┐
│  hermes    │──── LLM 请求 ────────►│ SkillClaw Proxy (:30000)        │
│            │◄─── LLM 回复 ────────│   ↓ 录制 session                │
│            │                       │   ↓ 上传到 OpenViking           │
│            │──── 记忆读写 ────────►│ OpenViking (:1933)              │
│            │◄─── 搜索/读取 ───────│   记忆 + 技能 + sessions        │
│            │                       │                                 │
│            │                       │ evolve_server (每10分钟)         │
│            │                       │   session → LLM分析 → skill     │
│            │                       │                                 │
│            │                       │ DreamCycle (每晚0-6点)           │
│            │                       │   去重/归档/维护团队概况          │
└────────────┘                       └─────────────────────────────────┘
```

**数据流**：
1. 你跟 hermes 对话 → 请求经 SkillClaw proxy 转发给 LLM
2. Proxy 自动录制对话为 session → 上传 OpenViking
3. evolve_server 消费 session → 调 LLM 分析 → 进化出 skill
4. 下次你（或团队任何人）使用 hermes 时，自动加载这些 skill
5. DreamCycle 每晚整理知识库，保持团队信息新鲜

---

## 快速使用指南

### 前提

- macOS（Python 3.11+）
- 能访问 `10.37.243.72`（内网）

### 3 步上手

**Step 1：安装 hermes**

```bash
git clone <内部仓库地址> hermes-agent
cd hermes-agent
pip install -e .
```

**Step 2：配置环境**

```bash
mkdir -p ~/.hermes
cp env.template ~/.hermes/.env
```

然后编辑 `~/.hermes/.env`，**只改一行**：

```bash
OPENVIKING_USER=<你的拼音名字>
# 例如：OPENVIKING_USER=wangzong 或 OPENVIKING_USER=lige
```

**Step 3：启动**

```bash
hermes
```

就这样，开箱即用。

---

## 你能做什么

### 记忆系统（自动工作）

agent 会自动记住你说过的事情：

- **个人记忆**：你的偏好、习惯、项目上下文（只有你能看到）
- **团队记忆**：SOP、技术方案、最佳实践（全组可见）

你也可以主动操作：

```
你：帮我记住 xxx 项目的部署流程是 yyy
你：搜索团队记忆中关于"部署"的内容
你：删除之前记的那条过时的信息
```

### 技能进化（全自动）

你正常使用就行。系统会观察大家的对话，发现：
- agent 重复犯的错 → 自动生成纠正 skill
- 大家共同的使用模式 → 提炼为最佳实践 skill

**已进化的示例 skill**：

> `use-viking-search-and-forget-tools`
> 
> 起因：agent 反复用错 viking_search 的参数
> 效果：后续所有人的对话中，agent 自动知道正确用法

### 团队知识搜索

新人入职后，hermes 就能告诉你：
- 团队在做什么
- 谁负责什么
- 常用的服务地址
- 历史的技术决策

因为这些都沉淀在 OpenViking 的团队空间里了。

---

## 记忆隔离说明

| 内容类型 | 存储位置 | 谁能看到 |
|---------|---------|---------|
| "我叫 XXX"、个人偏好 | `viking://user/<你>/` | 只有你 |
| 技术方案、SOP、部署流程 | `viking://user/__team__/` | 全组 |
| 对话 session | `viking://resources/skillclaw/team-a/sessions/` | 系统消费后删除 |
| 进化的 skill | `viking://resources/skillclaw/team-a/skills/` | 全组自动加载 |

**不用担心隐私**：个人信息绝不会泄露到团队空间。

---

## FAQ

**Q: 需要一直保持某个终端运行吗？**

不需要。所有后台服务（OpenViking、SkillClaw proxy、evolve_server、DreamCycle）都跑在服务器上。你只管开 hermes 用就行。

**Q: 断网/关机后记忆会丢失吗？**

不会。所有记忆存在服务器的 OpenViking 里，持久化。

**Q: 我不想被录制对话怎么办？**

改 `~/.hermes/.env` 把 `OPENAI_BASE_URL` 直接指向 ark（绕过 proxy）：

```bash
OPENAI_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
```

这样记忆还能用，但对话不会被进化系统消费。

**Q: 怎么看当前有哪些团队 skill？**

```
你：帮我看看团队有哪些技能
```

或直接问 agent 任何关于团队知识库的问题。

**Q: 进化出的 skill 质量不好怎么办？**

可以让 agent 标记反馈：

```
你：上次那个关于 xxx 的建议不太对，帮我反馈一下
```

---

## 管理员参考

服务器 `10.37.243.72`（用户 `zhangpengkun`）上的服务：

| 服务 | 启动命令 | 端口 |
|------|---------|------|
| OpenViking | `cd ~/OpenViking && ./run.sh` | 1933 |
| SkillClaw Proxy | `cd ~/SkillClaw && skillclaw start` | 30000 |
| evolve_server | `cd ~/SkillClaw && python -m evolve_server` | 8787 |
| DreamCycle | `cd ~/DreamCycle && dreamcycle --daemon` | - |

```bash
# 手动触发一次进化（调试用）
cd ~/SkillClaw
set -a; source evolve_server/.env; set +a
python -m evolve_server --once

# 查看 DreamCycle 状态
cd ~/DreamCycle
dreamcycle --status

# 清理孤儿 skill
# （参考内部运维文档）
```

---

## 联系

- 项目维护：刘越 / 张鹏鲲
- 服务器管理：张鹏鲲
- 有问题直接在群里问，或者直接问 hermes —— 它知道的比你想象的多
