const test = require("node:test");
const assert = require("node:assert/strict");

const {
  focusPrimaryWindow,
  handleBeforeQuit,
  handleWindowAllClosed,
  wireSingleInstanceLifecycle,
} = require("../dist-electron/electron/app_lifecycle.js");

test("focusPrimaryWindow restores minimized window and focuses it", () => {
  const calls = [];
  const window = {
    isMinimized: () => true,
    restore: () => calls.push("restore"),
    show: () => calls.push("show"),
    focus: () => calls.push("focus"),
  };

  focusPrimaryWindow([window]);

  assert.deepEqual(calls, ["restore", "show", "focus"]);
});

test("wireSingleInstanceLifecycle quits immediately without single-instance lock", () => {
  let quitCalled = 0;
  const app = {
    quit: () => {
      quitCalled += 1;
    },
    on: () => {
      throw new Error("should not register listeners when lock is missing");
    },
  };

  wireSingleInstanceLifecycle({
    app,
    hasSingleInstanceLock: false,
    focusPrimaryWindow: () => undefined,
  });

  assert.equal(quitCalled, 1);
});

test("wireSingleInstanceLifecycle focuses the primary window on second-instance", () => {
  const listeners = new Map();
  let focusCount = 0;
  const app = {
    quit: () => undefined,
    on: (event, listener) => {
      listeners.set(event, listener);
    },
  };

  wireSingleInstanceLifecycle({
    app,
    hasSingleInstanceLock: true,
    focusPrimaryWindow: () => {
      focusCount += 1;
    },
  });

  listeners.get("second-instance")();
  assert.equal(focusCount, 1);
});

test("handleWindowAllClosed quits on non-darwin platforms", () => {
  let quitCalled = 0;
  handleWindowAllClosed({
    app: {
      quit: () => {
        quitCalled += 1;
      },
    },
    platform: "linux",
  });
  assert.equal(quitCalled, 1);
});

test("handleWindowAllClosed keeps app alive on darwin", () => {
  let quitCalled = 0;
  handleWindowAllClosed({
    app: {
      quit: () => {
        quitCalled += 1;
      },
    },
    platform: "darwin",
  });
  assert.equal(quitCalled, 0);
});

test("handleBeforeQuit prevents default once and shuts the controller down", async () => {
  let prevented = 0;
  let shutdownCalled = 0;
  let quitCalled = 0;
  const state = { isShuttingDown: false };

  await handleBeforeQuit({
    event: {
      preventDefault: () => {
        prevented += 1;
      },
    },
    state,
    controller: {
      shutdown: async () => {
        shutdownCalled += 1;
      },
    },
    quit: () => {
      quitCalled += 1;
    },
  });

  assert.equal(prevented, 1);
  assert.equal(shutdownCalled, 1);
  assert.equal(quitCalled, 1);
  assert.equal(state.isShuttingDown, true);
});

test("handleBeforeQuit is idempotent once shutdown has started", async () => {
  let prevented = 0;
  let shutdownCalled = 0;
  let quitCalled = 0;
  const state = { isShuttingDown: true };

  await handleBeforeQuit({
    event: {
      preventDefault: () => {
        prevented += 1;
      },
    },
    state,
    controller: {
      shutdown: async () => {
        shutdownCalled += 1;
      },
    },
    quit: () => {
      quitCalled += 1;
    },
  });

  assert.equal(prevented, 0);
  assert.equal(shutdownCalled, 0);
  assert.equal(quitCalled, 0);
});
