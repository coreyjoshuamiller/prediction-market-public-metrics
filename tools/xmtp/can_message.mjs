import { Client, IdentifierKind } from "@xmtp/node-sdk";
const addrs = process.argv.slice(2);
const ids = addrs.map((a) => ({ identifier: a.toLowerCase(), identifierKind: IdentifierKind.Ethereum }));
const res = await Client.canMessage(ids, "production");
const out = {};
for (const [k, v] of res) out[k.toLowerCase()] = v;
console.log(JSON.stringify(out));
