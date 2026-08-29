const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const root = path.join(__dirname, '..');
const scriptSrc = fs.readFileSync(path.join(root, 'script.js'), 'utf8');
const helperSrc = scriptSrc.slice(0, scriptSrc.indexOf('document.addEventListener'));
assert.ok(helperSrc.includes('installPersonalWatchlistHelpers'), 'script.js is missing watchlist helpers');

const context = { console };
context.globalThis = context;
vm.createContext(context);
vm.runInContext(helperSrc, context);
const PersonalWatchlist = context.PersonalWatchlist;
assert.ok(PersonalWatchlist, 'PersonalWatchlist helpers did not install');

function host(value) {
    return JSON.parse(JSON.stringify(value));
}

function testParseProjectWatchlist() {
    const payload = JSON.parse(fs.readFileSync(path.join(root, 'watchlists/personal_watchlist.json'), 'utf8'));
    const parsed = PersonalWatchlist.parseWatchlistPayload(payload);
    assert.strictEqual(parsed.schemaVersion, 1);
    assert.strictEqual(parsed.productIds.length, 174);
    assert.ok(parsed.productIds.includes('22589154150'));
    parsed.productIds.forEach(id => {
        assert.strictEqual(id, PersonalWatchlist.normalizeProductId(id));
        assert.strictEqual(typeof id, 'string');
        assert.ok(/^[1-9][0-9]*$/.test(id));
    });
}

function testNormalizeRejectsInvalidIds() {
    [
        'a', 'b', 'keep-me', '1e5', '100.0', '01', '0', '-1', '  ',
        '', null, undefined, true, {}, [], NaN, Infinity, -3, 0, 1.5,
        Number.MAX_SAFE_INTEGER + 1
    ].forEach(value => {
        assert.strictEqual(PersonalWatchlist.normalizeProductId(value), '');
    });
    assert.strictEqual(PersonalWatchlist.normalizeProductId(100), '100');
    assert.strictEqual(PersonalWatchlist.normalizeProductId(' 22589154150 '), '22589154150');
    assert.strictEqual(PersonalWatchlist.normalizeProductId('9007199254740993'), '9007199254740993');
}

function testParseRejectsBadPayloads() {
    const existing = ['1'];
    const cases = [
        null,
        [],
        { schemaVersion: 2, productIds: ['1'] },
        { schemaVersion: 1, productIds: [] },
        { schemaVersion: 1, productIds: ['', '  ', null] },
        { schemaVersion: 1, productIds: ['a', '100.0', '1e5', -1, 1.5] },
        { productIds: ['1'] }
    ];
    cases.forEach(value => {
        assert.throws(() => PersonalWatchlist.parseWatchlistPayload(value));
    });
    assert.throws(() => JSON.parse('{not json'));
    assert.deepStrictEqual(existing, ['1']);
}

function testDedupeAndNormalize() {
    const parsed = PersonalWatchlist.parseWatchlistPayload({
        schemaVersion: 1,
        productIds: [' 100 ', 100, '100.0', '100', '200', 'abc', '1e5']
    });
    assert.deepStrictEqual(host(parsed.productIds), ['100', '200']);
}

function testScopeLeavesResultsAloneWhenDisabledOrEmpty() {
    const products = { '100': { name: 1 }, '200': { name: 2 } };
    assert.strictEqual(PersonalWatchlist.applyWatchlistScope(products, ['100'], false), products);
    assert.strictEqual(PersonalWatchlist.applyWatchlistScope(products, [], true), products);
    const scoped = PersonalWatchlist.applyWatchlistScope(products, ['200'], true);
    assert.deepStrictEqual(host(Object.keys(scoped)), ['200']);
    assert.strictEqual(scoped['200'], products['200']);
    assert.ok(!scoped['100']);
    assert.ok(products['100'], 'original search results must stay intact');
}

function testMatchCountsAndStorageFailSafe() {
    const products = { '22589154150': {}, '999999': {} };
    const counts = PersonalWatchlist.watchlistMatchCounts(products, ['22589154150', '888']);
    assert.deepStrictEqual(host(counts), { imported: 2, matched: 1, missed: 1 });

    const storage = {
        data: { inventoryPersonalWatchlistIds: '{bad', inventoryPersonalWatchlistEnabled: '1' },
        getItem(key) { return this.data[key]; },
        setItem(key, value) { this.data[key] = value; }
    };
    assert.deepStrictEqual(host(PersonalWatchlist.readStoredWatchlist(storage)), { productIds: [], enabled: false });
    assert.strictEqual(PersonalWatchlist.writeStoredWatchlist(storage, [' 1 ', '1', '2'], true), true);
    assert.deepStrictEqual(host(PersonalWatchlist.readStoredWatchlist(storage)), { productIds: ['1', '2'], enabled: true });

    const throwing = {
        getItem() { return null; },
        setItem() { throw new Error('quota'); }
    };
    assert.strictEqual(PersonalWatchlist.writeStoredWatchlist(throwing, ['1'], true), false);
}

testParseProjectWatchlist();
testNormalizeRejectsInvalidIds();
testParseRejectsBadPayloads();
testDedupeAndNormalize();
testScopeLeavesResultsAloneWhenDisabledOrEmpty();
testMatchCountsAndStorageFailSafe();
console.log('test_personal_watchlist.js ok');
