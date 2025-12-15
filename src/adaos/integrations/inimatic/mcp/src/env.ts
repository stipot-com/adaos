function env(name: string): string | undefined {
    const v = process.env[name];
    return typeof v === 'string' ? v : undefined;
  }
  
  export const BACKEND_URL = env('INIMATIC_BACKEND_URL');
  export const ROOT_TOKEN = env('INIMATIC_ROOT_TOKEN');
  export const TLS_INSECURE = env('INIMATIC_TLS_INSECURE') === '1';
  
  if (!BACKEND_URL) {
    throw new Error('INIMATIC_BACKEND_URL is required');
  }
  