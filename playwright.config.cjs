'use strict';
const {defineConfig, devices} = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests/browser',
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  timeout: 30000,
  expect: {timeout: 6000},
  reporter: process.env.CI ? 'line' : 'list',
  outputDir: 'test-results/browser',
  use: {
    baseURL: 'http://127.0.0.1:18765',
    acceptDownloads: true,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [{name: 'chromium', use: {...devices['Desktop Chrome']}}],
  webServer: {
    command: 'node tests/browser/fixture_server.cjs',
    url: 'http://127.0.0.1:18765/__fixture__/health',
    reuseExistingServer: false,
    timeout: 10000,
  },
});
