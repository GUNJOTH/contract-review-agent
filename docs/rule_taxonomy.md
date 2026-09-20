# 规则功能分类（按"判什么"归域）

本文件是对规则包全量 **57 条**规则的**功能分类**——按"这条规则在判断什么"归域。

它**不替代**规则自带的 `category` 字段：`category` 是规则来源表的分组痕迹（如
`Qx新技术架构描述相关`、`软件开发服务合同（0税率）重点检查项`），粒度不齐、
且混装了不同判断对象（"违约责任明确"被归在"合同主体"下）。功能域是给
"按域组织模型调用"用的稳定分组。

- 规则来源：`data/contract_rules_v0.14.json`（50 条）+ `data/contract_core_rules_v0.15.json`（7 条）
- 覆盖校验：10 域合计 57 条，无重复、无遗漏
- 判定方式（`check_method`）：`deterministic` 18 / `semantic` 25 / `keyword` 5 / `classification` 5 / `human` 3 / `visual` 1

## 一、十域总览

| 域 | 条数 | 判定方式构成 | 需模型判 | 判断的是什么 |
|---|---|---|---|---|
| **D1 合同定性** | 7 | classification×5, semantic×2 | 7 | 这是份什么合同、叫什么名字 |
| **D2 金额与税务** | 14 | deterministic×12, semantic×2 | 2 | 钱算得对不对（金额/税率/发票/付款） |
| **D3 交付与验收** | 6 | semantic×4, deterministic×2 | 4 | 交什么、何时交、怎么算完成 |
| **D4 知识产权** | 5 | human×3, semantic×2 | 2 | 成果归谁、权利如何行使 |
| **D5 技术架构** | 7 | semantic×7 | 7 | 技术方案描述是否到位 |
| **D6 源码交付** | 5 | keyword×5 | 0 | 是否涉及源代码交付（本地检索） |
| **D7 责任与救济** | 5 | deterministic×3, semantic×2 | 2 | 违约怎么办、合同如何结束 |
| **D8 形式要件与完整性** | 6 | semantic×5, visual×1 | 5 | 条款/附件/签章是否齐备合规 |
| **D9 保密与数据** | 1 | semantic×1 | 1 | 技术情报与资料保密 |
| **D10 跨文档一致性** | 1 | deterministic×1 | 0 | 多份文档间关键事实是否一致 |
| **合计** | **57** | | **30** | |

要点：**57 条里只有 30 条需要模型判**（25 semantic + 5 classification）。其余 27 条
由引擎本地算（deterministic 18）、关键词检索（keyword 5）、人工复核（human 3）、
视觉检查（visual 1）负责。

## 二、逐域明细

### D1 合同定性（7）

| rule_id | 标题 | 方式 | 原 category |
|---|---|---|---|
| CONTRACT-CHECK-0D566D1140D9 | 软件产品销售 | classification | 合同类型 |
| CONTRACT-CHECK-66F4FDEE8852 | 软件开发/转让服务 | classification | 合同类型 |
| CONTRACT-CHECK-65C73394F4BA | 一般商品销售合同 | classification | 合同类型 |
| CONTRACT-CHECK-7EDAE6E42319 | 混合合同 | classification | 合同类型 |
| CONTRACT-CHECK-99BE9ADE3773 | 其它服务合同 | classification | 合同类型 |
| CONTRACT-CHECK-97AB266DCBA7 | 合同名称 | semantic | 合同主体 |
| CONTRACT-CHECK-5022DA193A57 | 封面：技术开发合同 | semantic | 软件开发服务合同（0税率）重点检查项 |

> 前 5 条是**同一个单选题的 5 个选项**，不是一个判断的 5 个独立实例。

### D2 金额与税务（14）

| rule_id | 标题 | 方式 | 原 category |
|---|---|---|---|
| CONTRACT-CHECK-875D2A258280 | 金额大小写一致 | deterministic | 金额 |
| CONTRACT-CHECK-5E14216899C4 | 金额明细汇总一致 | deterministic | 金额 |
| CONTRACT-CHECK-2C24E4374821 | 不含税 | deterministic | 金额 |
| CONTRACT-CHECK-CBAFDF262EB8 | 税率 | deterministic | 金额 |
| CONTRACT-CHECK-1BF7A165D755 | 税额 | deterministic | 金额 |
| CONTRACT-CHECK-8A7E00066A65 | 付款方式（付款比例） | deterministic | 付款 |
| CONTRACT-CHECK-4CABB10596A5 | 是否有保函要求？ | deterministic | 付款 |
| CONTRACT-CHECK-6DFB2B3C395A | 付款总额是否与合同额相等 | deterministic | 付款 |
| CONTRACT-CHECK-51E4F72FF9D1 | 发票类型 | deterministic | 发票 |
| CONTRACT-CHECK-F57828183B8B | 发票金额 | deterministic | 发票 |
| CONTRACT-CHECK-41B683F2356A | 发票总额是否与合同额相等 | deterministic | 发票 |
| CONTRACT-CHECK-1AD81446696C | 成本估算 | semantic | 合规性/交付问题 |
| CONTRACT-CHECK-999C61BD8B59 | 开具技术开发服务发票（税率为0的普票，非专票） | semantic | 软件开发服务合同（0税率）重点检查项 |
| CONTRACT-CORE-PAYMENT-001 | 付款条件与节点 | deterministic | 付款 |

### D3 交付与验收（6）

| rule_id | 标题 | 方式 | 原 category |
|---|---|---|---|
| CONTRACT-CHECK-81DA4685E47F | 工期/交付日期 | semantic | 合规性/交付问题 |
| CONTRACT-CHECK-EE73FB5AECCA | 履行的计划、进度、期限、地点、地域和方式 | semantic | 软件开发服务合同（0税率）重点检查项 |
| CONTRACT-CHECK-274FC4247520 | 验收标准和方法 | semantic | 软件开发服务合同（0税率）重点检查项 |
| CONTRACT-CHECK-44EA101C6E2C | 如果是升级、优化、扩展可能会需要提供第一次开发合同 | semantic | 软件开发服务合同（0税率）重点检查项 |
| CONTRACT-CORE-DELIVERY-001 | 交付与履行期限 | deterministic | 交付 |
| CONTRACT-CORE-ACCEPTANCE-001 | 验收标准与方法 | deterministic | 验收 |

### D4 知识产权（5）

| rule_id | 标题 | 方式 | 原 category |
|---|---|---|---|
| CONTRACT-CHECK-AD9C3769093E | 产权归属 | human | 知识产权 |
| CONTRACT-CHECK-9CCBE1B516D5 | 专利 | human | 知识产权 |
| CONTRACT-CHECK-1DA17C10920C | 期刊 | semantic | 知识产权 |
| CONTRACT-CHECK-68FD46A56186 | 著作权名称 | semantic | 知识产权 |
| CONTRACT-CHECK-774388E91C1A | 技术成果归属和收益的分成办法 | human | 软件开发服务合同（0税率）重点检查项 |

### D5 技术架构（7）

| rule_id | 标题 | 方式 | 原 category |
|---|---|---|---|
| CONTRACT-CHECK-AA61E0FE61E2 | 微服务 | semantic | Qx新技术架构描述相关 |
| CONTRACT-CHECK-A48023900111 | 信创 | semantic | Qx新技术架构描述相关 |
| CONTRACT-CHECK-6C794D5A861E | 国产化 | semantic | Qx新技术架构描述相关 |
| CONTRACT-CHECK-0717EBD030A4 | 云架构 | semantic | Qx新技术架构描述相关 |
| CONTRACT-CHECK-D287A323E291 | 大数据 | semantic | Qx新技术架构描述相关 |
| CONTRACT-CHECK-157FEC3F335B | 数据治理 | semantic | Qx新技术架构描述相关 |
| CONTRACT-CHECK-7816F4A0A226 | 补充，业绩和控标用… | semantic | Qx新技术架构描述相关 |

> 7 条共享同一个证据池（技术方案/架构描述章节），是"同一次通读要看的 7 个要素"。

### D6 源码交付（5）

| rule_id | 标题 | 方式 | 原 category |
|---|---|---|---|
| CONTRACT-CHECK-1EB03A0ED5A0 | 源代码 | keyword | 源代码相关（按关键字搜索） |
| CONTRACT-CHECK-F036278C7CB9 | 源程序 | keyword | 源代码相关（按关键字搜索） |
| CONTRACT-CHECK-997A98C9285D | 源码 | keyword | 源代码相关（按关键字搜索） |
| CONTRACT-CHECK-150551B0F63E | 代码 | keyword | 源代码相关（按关键字搜索） |
| CONTRACT-CHECK-16BC0E4573FB | 程序 | keyword | 源代码相关（按关键字搜索） |

> 5 条是**同一族检索词**，本地匹配即可，不需要模型。

### D7 责任与救济（5）

| rule_id | 标题 | 方式 | 原 category |
|---|---|---|---|
| CONTRACT-CHECK-6D59A5035735 | 违约责任明确 | semantic | 合同主体 |
| CONTRACT-CHECK-BB629A656600 | 风险责任承担 | semantic | 软件开发服务合同（0税率）重点检查项 |
| CONTRACT-CORE-RENEWAL-001 | 续期与续签 | deterministic | 续期 |
| CONTRACT-CORE-TERMINATION-001 | 解除与终止 | deterministic | 终止 |
| CONTRACT-CORE-BREACH-001 | 违约责任与责任范围 | deterministic | 违约责任 |

### D8 形式要件与完整性（6）

| rule_id | 标题 | 方式 | 原 category |
|---|---|---|---|
| CONTRACT-CHECK-62A5629DD18D | 合同完整性（无空白等） | semantic | 合规性/交付问题 |
| CONTRACT-CHECK-1B59A23E6377 | 合同（包括：技术协议及附件）必须盖骑缝章 | visual | 软件开发服务合同（0税率）重点检查项 |
| CONTRACT-CHECK-E1903FDBD368 | 技术协议 | semantic | 合同主体 |
| CONTRACT-CHECK-4C9A7D6FB7EC | 合同范围 | semantic | 合同主体 |
| CONTRACT-CHECK-3B7CD50964E7 | 标的内容、范围和要求 | semantic | 软件开发服务合同（0税率）重点检查项 |
| CONTRACT-CHECK-BACB2ECA9235 | 项目名称 | semantic | 软件开发服务合同（0税率）重点检查项 |

### D9 保密与数据（1）

| rule_id | 标题 | 方式 | 原 category |
|---|---|---|---|
| CONTRACT-CHECK-4D4FCC703F14 | 技术情报和资料的保密 | semantic | 软件开发服务合同（0税率）重点检查项 |

### D10 跨文档一致性（1）

| rule_id | 标题 | 方式 | 原 category |
|---|---|---|---|
| CONTRACT-CORE-CROSS-DOCUMENT-001 | 跨文档关键事实一致性 | deterministic | 跨文档一致性 |

## 三、模型调用的现状与可优化点

现状（`pipeline._review_semantic_rules_in_isolation`）：语义判据**逐规则**调用模型。
一份"软件开发/转让服务"合同适用 24 条语义规则 → **24 次模型调用**。串行还是并行由
`CONTRACT_REVIEW_SEMANTIC_MAX_CONCURRENCY` 决定（默认 1 即串行；语义客户端会按该值
自动敞开自己那份闸门容量，无需同时调整全局模型并发）。通读风险分析另算 4 次（分片，
同样可并发）。

按域组织后有四个层次的改动，收益与代价递增：

| 方案 | 调用次数 | 提速 | 代价 |
|---|---|---|---|
| 现状：逐规则串行 | 24～30 次 | — | — |
| ⓪ 打开语义并发（`SEMANTIC_MAX_CONCURRENCY=3`） | 24～30 次不变，在途 3 | ~2.7× | 无（不动判定结构，只改并行度） |
| ① 同族项合并（D1 的 5 选项、D5 的 7 要素） | 约 15 次 | ~2× | 低（本就是同一次判断） |
| ② 整域合并（D1～D9 各一次） | 8 次 | ~3× | 证据门禁从"规则级隔离"放宽到"域级隔离" |
| ③ 风险分析 `rule_hints` 按域分片 | 仍是 3～4 片 | 提速有限 | 低（替代现在的定长 15 条切片） |

⓪ 与 ①② 可叠加：先打开并发拿到约 2.7×，再做同族合并把往返轮次再降一半。

关于精度：
- **精度收益来自"同域同上下文"**——例如 D2 的金额五条（大小写/明细汇总/不含税/税率/税额）
  本质是同一笔钱的五个侧面，分开调用时模型无法用"大小写不一致"去佐证"税额有误"。
- **精度风险来自"证据隔离变松"**——现有架构刻意做规则级证据隔离
  （函数名 `..._in_isolation` 即为此），防止用 A 规则的证据去证 B 规则。②会把隔离粒度
  放宽到域级，审计可信度需要重新评估。
- ③ 是纯收益：现在的定长 15 条切片会把 D1 的 5 个选项、D5 的 7 个要素切散
  （第 15 条边界正落在 D2 中间），按域分片可让每片同域。
