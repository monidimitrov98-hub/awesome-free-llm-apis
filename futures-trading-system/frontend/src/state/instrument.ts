import { createContext, useContext } from "react";

export const InstrumentContext = createContext<{ instrument: string; setInstrument(r: string): void }>({
  instrument: "MES", setInstrument: () => {},
});
export function useInstrument() { return useContext(InstrumentContext); }
