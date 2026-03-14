import { EventEmitter } from "node:events";
import { Socket } from "node:net";
import { createInterface, type Interface } from "node:readline";

type RpcSuccessEnvelope = {
  id: string | null;
  ok: true;
  result: Record<string, unknown>;
};

type RpcErrorEnvelope = {
  id: string | null;
  ok: false;
  error: {
    code: string;
    message: string;
  };
};

type RpcEventEnvelope = {
  type: "event";
  event: string;
  payload: unknown;
};

type RpcEnvelope = RpcSuccessEnvelope | RpcErrorEnvelope | RpcEventEnvelope;

export class RpcClientError extends Error {
  code: string;

  constructor(message: string, code = "rpc_error") {
    super(message);
    this.name = "RpcClientError";
    this.code = code;
  }
}

function parseLine(line: string): RpcEnvelope {
  try {
    return JSON.parse(line) as RpcEnvelope;
  } catch (error) {
    throw new RpcClientError(`Invalid JSON from tiya-supervisor: ${String(error)}`, "invalid_json");
  }
}

function openSocket(): Socket {
  return new Socket();
}

export async function callRpc(method: string, params: Record<string, unknown>, socketPath: string): Promise<Record<string, unknown>> {
  const socket = openSocket();
  const request = JSON.stringify({
    id: "desktop",
    method,
    params
  });

  return new Promise((resolve, reject) => {
    const rl = createInterface({ input: socket });
    let settled = false;
    const rejectOnce = (error: RpcClientError) => {
      if (settled) {
        return;
      }
      settled = true;
      reject(error);
    };
    const onError = (error: Error) => {
      rl.close();
      socket.destroy();
      rejectOnce(new RpcClientError(error.message, "socket_error"));
    };

    socket.on("error", onError);
    rl.on("error", onError);
    rl.once("line", (line) => {
      if (settled) {
        return;
      }
      settled = true;
      socket.off("error", onError);
      rl.off("error", onError);
      rl.close();
      socket.end();

      const envelope = parseLine(line);
      if ("type" in envelope) {
        reject(new RpcClientError("Unexpected event payload during RPC call"));
        return;
      }
      if (!envelope.ok) {
        reject(new RpcClientError(envelope.error.message, envelope.error.code));
        return;
      }
      resolve(envelope.result);
    });

    socket.once("connect", () => {
      socket.write(`${request}\n`);
    });
    try {
      socket.connect(socketPath);
    } catch (error) {
      onError(error instanceof Error ? error : new Error(String(error)));
    }
  });
}

export class RpcSubscription extends EventEmitter {
  private socket: Socket | null = null;
  private rl: Interface | null = null;

  async connect(method: string, params: Record<string, unknown>, socketPath: string): Promise<Record<string, unknown>> {
    this.socket = openSocket();
    this.rl = createInterface({ input: this.socket });

    const initialResult = await new Promise<Record<string, unknown>>((resolve, reject) => {
      let settled = false;
      const rejectOnce = (error: RpcClientError) => {
        if (settled) {
          return;
        }
        settled = true;
        reject(error);
      };
      const onError = (error: Error) => {
        this.socket?.off("error", onError);
        this.rl?.off("error", onError);
        rejectOnce(new RpcClientError(error.message, "socket_error"));
      };
      this.socket?.once("error", onError);
      this.rl?.once("error", onError);
      this.rl?.once("line", (line) => {
        if (settled) {
          return;
        }
        settled = true;
        this.socket?.off("error", onError);
        this.rl?.off("error", onError);
        const envelope = parseLine(line);
        if ("type" in envelope) {
          reject(new RpcClientError("Unexpected event payload during subscription bootstrap"));
          return;
        }
        if (!envelope.ok) {
          reject(new RpcClientError(envelope.error.message, envelope.error.code));
          return;
        }
        resolve(envelope.result);
      });
      this.socket?.once("connect", () => {
        const request = JSON.stringify({
          id: "desktop-subscription",
          method,
          params
        });
        this.socket?.write(`${request}\n`);
      });
      try {
        this.socket?.connect(socketPath);
      } catch (error) {
        onError(error instanceof Error ? error : new Error(String(error)));
      }
    });

    this.rl.on("line", (line) => {
      const envelope = parseLine(line);
      if (!("type" in envelope)) {
        this.emit("response", envelope);
        return;
      }
      this.emit("event", {
        name: envelope.event,
        payload: envelope.payload
      });
    });

    this.socket.on("close", () => {
      this.emit("close");
    });

    this.socket.on("error", (error) => {
      this.emit("error", new RpcClientError(error.message, "socket_error"));
    });

    return initialResult;
  }

  close(): void {
    this.rl?.close();
    this.socket?.end();
    this.socket?.destroy();
    this.rl = null;
    this.socket = null;
  }
}
