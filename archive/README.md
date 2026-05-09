# v1 — Node.js（已归档）

> ⚠️ 本目录是 v1 Node.js 实现的归档，**不再维护**。新版本是 Claude Skill（Python），见 [项目根 README](../README.md)。

## 文件

- `alipay.js`, `wechat.js` — 原始 CLI 转换脚本
- `account_map.json` — 账户映射配置（占位示例）
- `package.json`, `package-lock.json` — npm 依赖
- `AGENTS.md` — 原开发指南

## 原用法

```bash
nvm install lts/fermium && nvm use lts/fermium
npm install
npm run alipay   # 或 npm run wechat
```

旧脚本仍可运行，但不再接受新功能。新特性都在项目根目录的 Skill 中迭代。

---

## 原始 README 内容

将支付宝&微信个人账单转换为 MoneyWiz 兼容的 csv 文件（中文格式）。

### Installation

node 版本 >= `v14.16.1` (`lts/fermium`)

```shell
nvm install lts/fermium
```

进入项目目录后：

```shell
npm install
```

### Usage

#### account_map.json

该文件为自定义的账户名称转换表，key 表示原始文件中账户名称中包含的字符串，value 则为对应转换到你 MoneyWiz 中已有的账户，示例：

```json
{
  "1234": "农行卡",
  "5678": "工行卡",
  "招行": "招商银行储蓄卡",
  "余额": "支付宝余额",
  "花呗": "花呗",
  "零钱": "微信零钱"
}
```

使用脚本前请自行修改。

#### run

```shell
npm run alipay
# 或
npm run wechat
```

运行脚本后，根据提示，直接将下载解压后的支付宝（微信）账单 .csv 文件，拖入 Terminal 窗口即可。
