# Alipay2Moneywiz
将支付宝&微信个人账单转换为MoneyWiz兼容的csv文件（中文格式）
## Installation
node版本 >= `v14.16.1` (`lts/fermium`)
```shell
$ nvm install lts/fermium
```
进入项目目录后：
```shell
$ npm install
```
## Usage
### account_map.json
该文件为自定义的账户名称转换表，key表示原始文件中账户名称中包含的字符串，value则为对应转换到你MoneyWiz中已有的账户，示例：
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

### run
```shell
$ npm run alipay
```
```shell
$ npm run wechat
```
运行脚本后，根据提示，直接将下载解压后的支付宝（微信）账单.csv文件，拖入Terminal窗口即可。