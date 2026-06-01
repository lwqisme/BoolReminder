// ── LEAPS GA Worker Integration ──────────────────────────────────────────
// Appended to strategy_parameter_lab_worker.js

importScripts('/static/leaps_ga_engine.js');

// Handle LEAPS GA messages
const leapsGaOriginalOnMessage = self.onmessage;
self.onmessage = (event) => {
  const message = event.data || {};
  if (message.type === 'leaps_ga') {
    handleLeapsGa(message).catch((error) => {
      postMessage({ type: 'leaps_ga_error', run_id: message.run_id || '', message: error.message, stack: error.stack });
    });
    return;
  }
  // Fall through to original handler
  if (leapsGaOriginalOnMessage) leapsGaOriginalOnMessage(event);
};

async function handleLeapsGa(message) {
  const { packet, run_id } = message;
  const { priceSeriesBySymbol, config, paramRanges } = packet || {};

  if (!priceSeriesBySymbol || !Object.keys(priceSeriesBySymbol).length) {
    postMessage({ type: 'leaps_ga_error', run_id, message: 'No price data' });
    return;
  }

  const cfg = config || {};
  const popSize = cfg.population_size || 30;
  const generations = cfg.generations || 15;
  const mutationRate = cfg.mutation_rate || 0.15;
  const crossoverRate = cfg.crossover_rate || 0.80;
  const elitismCount = cfg.elitism_count || 3;
  const tournamentSize = cfg.tournament_size || 4;
  const seed = cfg.seed;

  const ranges = leapsGaEngine.mergeRanges(paramRanges);

  if (seed != null) {
    // Simple seeded random (mulberry32)
    let s = seed | 0;
    Math.random = function() {
      s |= 0; s = s + 0x6D2B79F5 | 0;
      let t = Math.imul(s ^ s >>> 15, 1 | s);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }

  // Init population
  const seenKeys = new Set();
  const population = [];
  while (population.length < popSize) {
    const ind = leapsGaEngine.randomIndividual(ranges);
    if (!seenKeys.has(ind.key)) {
      seenKeys.add(ind.key);
      population.push(ind);
    }
  }

  let fitnesses = population.map(ind => leapsGaEngine.leapsFitnessFn(ind, priceSeriesBySymbol));

  const snapshots = [];
  let bestIndividual = population[0];
  let bestFitness = fitnesses[0];
  const allEvaluated = new Map();

  for (let gen = 0; gen < generations; gen++) {
    // Check for pause/cancel
    if (typeof paused !== 'undefined' && paused) {
      while (typeof paused !== 'undefined' && paused && typeof cancelled !== 'undefined' && !cancelled) {
        await new Promise(r => setTimeout(r, 200));
      }
    }
    if (typeof cancelled !== 'undefined' && cancelled) {
      postMessage({ type: 'leaps_ga_cancelled', run_id });
      return;
    }

    // Rank
    const ranked = population.map((ind, i) => [ind, fitnesses[i]]).sort((a, b) => b[1] - a[1]);
    population.length = 0;
    fitnesses.length = 0;
    for (const [ind, fit] of ranked) {
      population.push(ind);
      fitnesses.push(fit);
    }

    for (const [ind, fit] of ranked) {
      const existing = allEvaluated.get(ind.key);
      if (!existing || fit > existing[1]) {
        allEvaluated.set(ind.key, [ind, fit]);
      }
    }

    const genBest = fitnesses[0];
    const genAvg = fitnesses.reduce((a, b) => a + b, 0) / fitnesses.length;
    const genWorst = fitnesses[fitnesses.length - 1];

    if (genBest > bestFitness) {
      bestFitness = genBest;
      bestIndividual = population[0];
    }

    snapshots.push({
      generation: gen + 1,
      best_fitness: genBest,
      avg_fitness: genAvg,
      worst_fitness: genWorst,
      best_key: bestIndividual.key,
      best_params: {
        drawdown_threshold_pct: bestIndividual.drawdown_threshold_pct,
        entry_mode: bestIndividual.entry_mode,
        stage1_days: bestIndividual.stage1_days,
        stage1_profit: bestIndividual.stage1_profit,
        stage1_sell: bestIndividual.stage1_sell,
        stage2_days: bestIndividual.stage2_days,
        stage2_profit: bestIndividual.stage2_profit,
        stage2_sell: bestIndividual.stage2_sell,
      },
    });

    // Progress
    postMessage({
      type: 'leaps_ga_progress',
      run_id,
      generation: gen + 1,
      total_generations: generations,
      best_fitness: genBest,
      avg_fitness: genAvg,
      snapshot: snapshots[snapshots.length - 1],
    });

    // Elitism
    const elites = population.slice(0, elitismCount);
    const nextPopulation = [...elites];

    while (nextPopulation.length < popSize) {
      const p1 = leapsGaEngine.tournamentsSelect(population, fitnesses, tournamentSize);
      const p2 = leapsGaEngine.tournamentsSelect(population, fitnesses, tournamentSize);

      let child;
      if (Math.random() < crossoverRate) {
        child = leapsGaEngine.leapsCrossover(p1, p2, ranges);
      } else {
        child = p1;
      }
      child = leapsGaEngine.leapsMutate(child, mutationRate, ranges);
      nextPopulation.push(child);
    }

    population.length = 0;
    for (const ind of nextPopulation.slice(0, popSize)) population.push(ind);
    fitnesses = population.map(ind => leapsGaEngine.leapsFitnessFn(ind, priceSeriesBySymbol));
  }

  // Final results
  const rankedFinal = [...allEvaluated.entries()]
    .sort((a, b) => b[1][1] - a[1][1]);

  const finalPopulation = [];
  for (let rank = 0; rank < rankedFinal.length; rank++) {
    const [key, [ind, fit]] = rankedFinal[rank];
    const row = {
      rank: rank + 1, key, fitness: fit,
      drawdown_threshold_pct: ind.drawdown_threshold_pct,
      entry_mode: ind.entry_mode,
      stage1_days: ind.stage1_days,
      stage1_profit: ind.stage1_profit,
      stage1_sell: ind.stage1_sell,
      stage2_days: ind.stage2_days,
      stage2_profit: ind.stage2_profit,
      stage2_sell: ind.stage2_sell,
    };
    // Collect trade details for top 10
    if (rank < 10) {
      row.trade_details = collectTradeDetails(ind, priceSeriesBySymbol);
    }
    finalPopulation.push(row);
  }

  postMessage({
    type: 'leaps_ga_done',
    run_id,
    result: {
      snapshots,
      best: finalPopulation[0] || null,
      final_population: finalPopulation,
      total_evaluated: allEvaluated.size,
      config: { population_size: popSize, generations, mutation_rate: mutationRate, crossover_rate: crossoverRate, elitism_count: elitismCount, tournament_size: tournamentSize, seed },
    },
  });
}

function collectTradeDetails(individual, priceSeriesBySymbol) {
  const trades = [];
  for (const [symbol, prices] of Object.entries(priceSeriesBySymbol)) {
    // Pre-compute bollinger
    const bbFull = leapsGaEngine.bollingerLowerBand(prices, 22, 2.0);
    const bbByDate = {};
    for (const b of bbFull) bbByDate[b.date] = b.band;

    const entries = leapsGaEngine.detectLeapsEntries(prices, individual.drawdown_threshold_pct, individual.entry_mode);
    const stages = individual.toStages();
    for (const entry of entries) {
      const trade = leapsGaEngine.computeSellLadder(entry, prices, stages, 190, entry.price * 1.1);
      const allDates = [entry.date, ...trade.sell_events.map(se => se.date)];
      allDates.sort();
      const sliceStart = new Date(allDates[0]);
      sliceStart.setDate(sliceStart.getDate() - 60);
      const sliceEnd = new Date(allDates[allDates.length - 1]);
      sliceEnd.setDate(sliceEnd.getDate() + 30);

      const priceSeries = [];
      for (const [d, p] of prices) {
        if (d >= sliceStart.toISOString().slice(0, 10) && d <= sliceEnd.toISOString().slice(0, 10)) {
          const pt = { date: d, price: p };
          if (bbByDate[d] != null) pt.bollinger_lower = bbByDate[d];
          priceSeries.push(pt);
        }
      }

      trades.push({
        symbol,
        entry_date: entry.date,
        entry_price: entry.price,
        drawdown_pct: entry.drawdown_pct,
        bollinger_score: entry.bollinger_score,
        composite_score: entry.composite_score,
        sell_events: trade.sell_events,
        expired: trade.expired,
        total_roi_pct: trade.total_roi_pct,
        price_series: priceSeries,
      });
    }
  }
  return trades;
}
