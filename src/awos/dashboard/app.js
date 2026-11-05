// AgentWorkOS operator console - entry point. ES modules, no build step, no external assets (see js/).
import { boot } from './js/shell.js';

if (typeof window !== 'undefined' && typeof document !== 'undefined') boot();
