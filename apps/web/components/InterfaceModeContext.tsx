"use client";

import { createContext, useContext, useEffect, useMemo, useState } from "react";

export type InterfaceMode = "simple" | "advanced";

type InterfaceModeContextValue = {
  mode: InterfaceMode;
  setMode: (mode: InterfaceMode) => void;
};

const InterfaceModeContext = createContext<InterfaceModeContextValue | null>(null);

export function InterfaceModeProvider({ children }: { children: React.ReactNode }) {
  const [mode, setModeState] = useState<InterfaceMode>("simple");

  useEffect(() => {
    const stored = window.localStorage.getItem("keftrade-interface-mode");
    if (stored === "advanced" || stored === "simple") setModeState(stored);
  }, []);

  const value = useMemo<InterfaceModeContextValue>(() => ({
    mode,
    setMode(nextMode) {
      setModeState(nextMode);
      window.localStorage.setItem("keftrade-interface-mode", nextMode);
    }
  }), [mode]);

  return <InterfaceModeContext.Provider value={value}>{children}</InterfaceModeContext.Provider>;
}

export function useInterfaceMode() {
  const value = useContext(InterfaceModeContext);
  if (!value) throw new Error("useInterfaceMode must be used inside InterfaceModeProvider");
  return value;
}
