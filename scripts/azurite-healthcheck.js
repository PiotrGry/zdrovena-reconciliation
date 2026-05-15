'use strict';
const crypto = require('crypto');
const http = require('http');

const ACCOUNT = 'devstoreaccount1';
// Well-known public Azurite development credentials — not a secret
const KEY = 'Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==';
const VERSION = '2026-02-06';

const date = new Date().toUTCString();

// Azurite uses path-style URLs (/accountname/container/blob), so the SDK prepends
// /accountname to the URI path — which already contains /accountname/ — giving
// a doubled account name in the canonical resource. This matches how the Azure SDK
// computes SharedKey signatures for path-style storage endpoints.
const canonHeaders = `x-ms-date:${date}\nx-ms-version:${VERSION}\n`;
const canonResource = `/${ACCOUNT}/${ACCOUNT}/\ncomp:list`;
const stringToSign = `GET\n\n\n\n\n\n\n\n\n\n\n\n${canonHeaders}${canonResource}`;

const sig = crypto
  .createHmac('sha256', Buffer.from(KEY, 'base64'))
  .update(stringToSign)
  .digest('base64');

http.get(
  {
    host: 'localhost',
    port: 10000,
    path: `/${ACCOUNT}/?comp=list`,
    headers: {
      'x-ms-date': date,
      'x-ms-version': VERSION,
      Authorization: `SharedKey ${ACCOUNT}:${sig}`,
    },
  },
  (res) => process.exit(res.statusCode === 200 ? 0 : 1),
).on('error', () => process.exit(1));
