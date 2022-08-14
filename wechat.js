import * as fsp from "fs/promises";
import * as fs from "fs";
import { parse } from 'csv-parse/sync';
import { stringify } from 'csv-stringify/sync';
import * as readline from 'node:readline'

const ACCOUNT_MAP = JSON.parse(await fsp.readFile("account_map.json"));

function main() {
  // request input file path
  const query = readline.createInterface({
    input: process.stdin,
    output: process.stdout
  });
  query.question('\n***微信账单转换***\n\n\n请输入原csv文件路径:\n\n', (answer) => {
    const reg = /\\/g;
    mainProcess(answer.trim().replace(reg, ''));
    query.close();
  })
}

async function mainProcess(source) {
  // read line by line
  const fileStream = fs.createReadStream(source);
  const rl = readline.createInterface({
    input: fileStream,
  });
  // remove unused line of csv
  let numberOfDash = 0;
  let realContent = '';
  for await (let input of rl) {
    if (input.startsWith('--')) {
      numberOfDash++;
      continue;
    }
    if (numberOfDash > 0) {
      realContent += input + '\n';
    }
  }

  // parse the csv content to object
  const records = parse(realContent, {
    delimiter: ',',
    columns: true,
    trim: true,
  });

  // process all records
  const transactions = [];
  records.forEach(record => {
    let transaction = {};
    transaction['日期'] = parseDate(record['交易时间']);
    transaction['描述'] = record['商品'] == "/" ? record['交易类型'] : record['商品'];
    transaction['账户'] = mapAccount(record['支付方式']);
    const fee = isNaN(record['金额(元)']) ? record['金额(元)'].substring(1, record['金额(元)'].length) : record['金额(元)'];
    if (record['收/支'] == '其他') {
      // keep alipay's transfer process, because no wechat transfer record found yet
      transaction['交易对方'] = '';
      transaction['分类'] = '';
      transaction['转账'] = mapAccount(record['交易对方']);
      if (record['商品说明'].includes("还款")) {
        transaction['金额'] = (-Math.abs(fee)).toString();
      } else {
        transaction['金额'] = fee;
      }
    } else {
      transaction['交易对方'] = record['交易对方'];
      transaction['分类'] = record['交易类型'];
      transaction['转账'] = '';
      if (record['收/支'] == '支出') {
        transaction['金额'] = (-Math.abs(fee)).toString();
      } else if (record['收/支'] == '收入') {
        transaction['金额'] = fee;
      }
    }
    transaction['标签'] = '';
    transaction['备注'] = '';

    transactions.push(transaction);
  });

  // output to file
  const output = stringify(transactions, {
    header: true,
    columns: ['账户', '转账', '描述', '交易对方', '分类', '日期', '备注', '标签', '金额']
  })
  const sourceDir = source.slice(0, source.lastIndexOf('/') + 1);
  await fsp.writeFile(`${sourceDir + getOutputName()}`, output);
  console.log(`\n解析完成，输出路径: ${sourceDir + getOutputName()}`);
}


function parseDate(dateStr) {
  const dateObj = new Date(dateStr);
  return dateObj.toLocaleDateString();
  // const time = dateObj.toLocaleTimeString('zh-CN', {
  //   hourCycle: "h24",
  // });
  // transaction['日期'] = date;
  // transaction['时间'] = time;
}

function mapAccount(recordStr) {
  if (recordStr == "" || recordStr == "/") {
    return "微信零钱";
  }
  for (const k in ACCOUNT_MAP) {
    if (recordStr.includes(k)) {
      return ACCOUNT_MAP[k];
    }
  }
  return recordStr;
}

function getOutputName() {
  const now = new Date();
  const date = now.getFullYear() + '_' + (now.getMonth() + 1).toString() + '_' + now.getDate();
  return `微信账单_${date}.csv`;
}

// main().then(
//   () => process.exit(),
//   (err) => {
//     console.error(err);
//     process.exit(-1);
//   }
// );

main();