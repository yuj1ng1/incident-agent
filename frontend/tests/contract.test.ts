// @vitest-environment node
import { mkdtempSync, readFileSync, rmdirSync, unlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { expect, it } from 'vitest';

interface Schema {
  $ref?: string;
  type?: string;
  const?: string | boolean;
  enum?: (string | boolean)[];
  anyOf?: Schema[];
  properties?: Record<string, Schema>;
  required?: string[];
  items?: Schema;
  default?: unknown;
}
const contract = JSON.parse(readFileSync(new URL('./api-contract.json', import.meta.url), 'utf8')) as {
  responses: Record<string, Schema>;
  schemas: Record<string, Schema>;
  serialization: Record<string, { exclude_unset: boolean; exclude_defaults: boolean; exclude_none: boolean }>;
};

function schemaType(schema: Schema): string {
  if (schema.$ref) return 'Schema_' + schema.$ref.split('/').at(-1);
  if (schema.anyOf) return '(' + schema.anyOf.map(schemaType).join(' | ') + ')';
  if ('const' in schema) return JSON.stringify(schema.const);
  if (schema.enum) return schema.enum.map(value => JSON.stringify(value)).join(' | ');
  if (schema.type === 'array') return 'Array<' + schemaType(schema.items!) + '>';
  if (schema.type === 'object') return '{ ' + Object.entries(schema.properties || {}).map(([name, field]) => {
    // These responses include defaults. The Python test verifies live response
    // serialization flags, so a default cannot silently become an absent field.
    const optional = !schema.required?.includes(name) && !('default' in field);
    return JSON.stringify(name) + (optional ? '?' : '') + ': ' + schemaType(field);
  }).join('; ') + ' }';
  if (schema.type === 'integer' || schema.type === 'number') return 'number';
  if (['string', 'boolean', 'null'].includes(schema.type || '')) return schema.type!;
  throw new Error('Unsupported OpenAPI field: ' + JSON.stringify(schema));
}

it('checks actual TypeScript response types against live-checked OpenAPI, including nested fields, enums, optionality and nullability', () => {
  for (const flags of Object.values(contract.serialization)) {
    expect(flags).toEqual({ exclude_unset: false, exclude_defaults: false, exclude_none: false });
  }
  const typePath = fileURLToPath(new URL('../src/api/types.ts', import.meta.url)).replaceAll('\\', '/');
  const assertions = [
    ['IncidentPage', '/api/incidents'], ['IncidentDetail', '/api/incidents/{incident_id}'],
    ['SessionPage', '/api/sessions'], ['SessionDetail', '/api/sessions/{session_id}'],
  ].map(([name, path]) => 'type Check_' + name + ' = Assert<Equal<' + name + ', ' + schemaType(contract.responses[path]) + '>>;');
  // Flatten intersections in retained investigation records without widening
  // literal unions or dropping optional/nullable fields.
  const source = [
    'import type { IncidentPage, IncidentDetail, IncidentSummary, SessionPage, SessionDetail, SessionSummary, SessionStatus } from ' + JSON.stringify(typePath) + ';',
    'type Normalize<T> = T extends readonly unknown[] ? { [K in keyof T]: Normalize<T[K]> } : T extends object ? { [K in keyof T]: Normalize<T[K]> } : T;',
    'type Equal<A, B> = (<T>() => T extends Normalize<A> ? 1 : 2) extends (<T>() => T extends Normalize<B> ? 1 : 2) ? true : false;',
    'type Assert<T extends true> = T;',
    ...Object.entries(contract.schemas).map(([name, schema]) => 'type Schema_' + name + ' = ' + schemaType(schema) + ';'),
    ...assertions,
    'type Check_Status = Assert<Equal<SessionStatus, Schema_SessionStatus>>;',
    // Negative probes ensure the equality check rejects common drift cases.
    'type Reject_Missing = Assert<Equal<Omit<IncidentSummary, "service">, IncidentSummary> extends false ? true : false>;',
    'type Reject_Optional = Assert<Equal<Omit<IncidentSummary, "service"> & { service?: string }, IncidentSummary> extends false ? true : false>;',
    'type Reject_Null = Assert<Equal<Omit<SessionSummary, "last_error_category"> & { last_error_category: string }, SessionSummary> extends false ? true : false>;',
    'type Reject_Enum = Assert<Equal<Omit<SessionSummary, "status"> & { status: string }, SessionSummary> extends false ? true : false>;',
    'type Reject_Nested = Assert<Equal<Omit<SessionDetail, "limits"> & { limits: SessionDetail["limits"] & { drifted?: number } }, SessionDetail> extends false ? true : false>;',
  ].join('\n');
  // Compile temporary assertions with the project's installed compiler. No
  // compiler API dependency, checked-in generated TS, or Python in npm test.
  const directory = mkdtempSync(join(tmpdir(), 'incident-contract-'));
  const sourcePath = join(directory, 'contract.ts');
  const configPath = join(directory, 'tsconfig.json');
  try {
    writeFileSync(sourcePath, source);
    writeFileSync(configPath, JSON.stringify({ compilerOptions: {
      strict: true, noEmit: true, skipLibCheck: true, target: 'ES2022',
      module: 'ESNext', moduleResolution: 'Bundler', types: [],
    }, files: [sourcePath] }));
    const compiler = fileURLToPath(new URL('../node_modules/typescript/bin/tsc', import.meta.url));
    const result = spawnSync(process.execPath, [compiler, '--project', configPath], { encoding: 'utf8', timeout: 30000 });
    expect(result.error).toBeUndefined();
    expect(result.status, result.stdout + result.stderr).toBe(0);
  } finally {
    // Remove only the exact files created by this test.
    for (const path of [sourcePath, configPath]) {
      try { unlinkSync(path); } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
    }
    rmdirSync(directory);
  }
}, 35000);
