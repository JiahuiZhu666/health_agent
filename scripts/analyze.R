#!/usr/bin/env Rscript
# Fixed, reviewed analysis; no arbitrary formulas or user-supplied R code are evaluated.
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 4L) stop("Usage: analyze.R adult.csv spec.json output.json registry.json")
suppressPackageStartupMessages(library(jsonlite))
write_result <- function(x) write_json(x, args[[3]], pretty = TRUE, auto_unbox = TRUE,
                                     null = "null", na = "null", digits = 15)
blocked <- function(code, reason) list(status = "blocked", code = code, reason = reason,
                                     rows = list(), checks = list())
# Only registry-selected response values are recoded; missing/skip values stay NA.
recode_binary <- function(raw, one, zero) {
  answer <- rep(NA_real_, length(raw))
  answer[!is.na(raw) & raw %in% zero] <- 0
  answer[!is.na(raw) & raw %in% one] <- 1
  answer
}
# Formulas contain only fixed, reviewed variable names, never user formula text.
fixed_model_formula <- function(adjust_income) {
  covariates <- c("education", "age", "sex", "race", if (adjust_income) "income")
  reformulate(covariates, response = "outcome_value")
}
warning_log <- character()
main <- function() {
  suppressPackageStartupMessages(library(survey))
  suppressPackageStartupMessages(library(mitools))
  options(survey.lonely.psu = "fail", survey.adjust.domain.lonely = FALSE)
  spec <- read_json(args[[2]], simplifyVector = FALSE)
  registry <- read_json(args[[4]], simplifyVector = FALSE)
  if (spec$year != 2024 || !is.character(spec$outcome) || length(spec$outcome) != 1L ||
      !spec$outcome %in% c("fair_poor", "insured", "uninsured", "delayed_care_cost", "unmet_care_cost") ||
      !spec$outcome %in% names(registry$outcomes)) stop("Unverified year or outcome")
  outcome <- registry$outcomes[[spec$outcome]]
  outcome_variable <- outcome$variable
  one_codes <- as.numeric(unlist(outcome$one))
  zero_codes <- as.numeric(unlist(outcome$zero))
  missing_codes <- as.numeric(unlist(outcome$missing))
  if (!is.character(outcome_variable) || length(outcome_variable) != 1L ||
      !outcome_variable %in% c("PHSTAT_A", "NOTCOV_A", "MEDDL12M_A", "MEDNG12M_A") ||
      !length(one_codes) || !length(zero_codes) ||
      any(!is.finite(c(one_codes, zero_codes, missing_codes))) ||
      anyDuplicated(c(one_codes, zero_codes, missing_codes))) stop("Invalid registered binary outcome coding")
  if (!spec$analysis %in% c("proportion", "gradient", "regression")) stop("Unsupported analysis")
  if (spec$min_n < 100) stop("Minimum denominator cannot be below 100")
  needed <- c("SRVY_YR", "RECTYPE", "HHX", "WTFA_A", "PSTRAT", "PPSU", "AGEP_A",
              "PHSTAT_A", "EDUCP_A", "HISPALLP_A", "SEX_A")
  needed <- unique(c(needed, outcome_variable))
  structural <- c("SRVY_YR", "RECTYPE", "HHX", "WTFA_A", "PSTRAT", "PPSU")
  read_selected <- function(path, keep) {
    header <- names(read.csv(path, nrows = 0L, check.names = FALSE))
    if (!all(keep %in% header) || anyDuplicated(header)) stop("Invalid official CSV columns")
    read.csv(path, colClasses = ifelse(header %in% keep, "character", "NULL"),
             check.names = FALSE, stringsAsFactors = FALSE, na.strings = c("", "NA"))
  }
  base <- read_selected(args[[1]], needed)
  for (v in setdiff(needed, "HHX")) base[[v]] <- as.numeric(base[[v]])
  if (nrow(base) != registry$datasets$adult_2024$expected_rows || anyNA(base[structural]) ||
      any(base$SRVY_YR != 2024) || any(base$RECTYPE != 10) || anyDuplicated(base$HHX))
    stop("Expected the complete unique 2024 Sample Adult release")
  if (any(!is.finite(base$WTFA_A) | base$WTFA_A <= 0)) stop("Invalid survey weights")
  psus <- lapply(split(base$PPSU, base$PSTRAT), unique)
  if (any(lengths(psus) < 2)) stop("Full-sample singleton PSU stratum: variance estimation blocked")
  recode <- function(raw, variable) {
    groups <- registry$groups[[variable]]$categories
    answer <- rep(NA_character_, length(raw))
    for (g in groups) {
      yes <- if (!is.null(g$codes)) raw %in% unlist(g$codes) else
        !is.na(raw) & raw >= g$minimum & raw <= g$maximum
      answer[yes] <- g$id
    }
    factor(answer, levels = vapply(groups, function(g) g$id, character(1)))
  }
  base$outcome_value <- recode_binary(base[[outcome_variable]], one_codes, zero_codes)
  base$education <- recode(base$EDUCP_A, "education")
  base$race <- recode(base$HISPALLP_A, "race")
  base$sex <- recode(base$SEX_A, "sex")
  base$age <- recode(base$AGEP_A, "age")
  base$age_years <- ifelse(base$AGEP_A %in% 18:85, base$AGEP_A, NA_real_)
  adjust_income <- spec$analysis == "regression" && spec$adjustment == "full"
  requires_mi <- spec$group_by == "income" || "income" %in% names(spec$filters) || adjust_income
  if (requires_mi && (is.null(spec$income_csv_path) || !file.exists(spec$income_csv_path)))
    return(blocked("income_data_missing", "All 10 official income imputations are required. Download adultinc24csv.zip; single-imputation fields are never substituted."))
  datasets <- list()
  if (requires_mi) {
    inc <- read_selected(spec$income_csv_path, c("RECTYPE", "HHX", "IMPNUM_A", "RATCAT_A"))
    for (v in c("RECTYPE", "IMPNUM_A", "RATCAT_A")) inc[[v]] <- as.numeric(inc[[v]])
    if (nrow(inc) != nrow(base) * 10L || anyNA(inc) || any(inc$RECTYPE != 30) ||
        !identical(sort(unique(inc$IMPNUM_A)), as.numeric(1:10)) || any(!inc$RATCAT_A %in% 1:14) ||
        anyDuplicated(paste(inc$HHX, inc$IMPNUM_A, sep = ":")))
      stop("Income file must have unique HHX × IMPNUM_A keys, all 10 imputations, and valid RATCAT_A codes")
    for (m in 1:10) {
      one <- inc[inc$IMPNUM_A == m, , drop = FALSE]
      if (nrow(one) != nrow(base) || !setequal(one$HHX, base$HHX)) stop("Income household merge is incomplete")
      d <- base
      d$income <- recode(one$RATCAT_A[match(d$HHX, one$HHX)], "income")
      datasets[[m]] <- d
    }
    rm(inc)
  } else {
    base$income <- factor(NA_character_, levels = vapply(registry$groups$income$categories, function(g) g$id, character(1)))
    datasets <- list(base)
  }
  age_counts <- numeric()
  designs <- full_designs <- list()
  for (m in seq_along(datasets)) {
    d <- datasets[[m]]
    d$domain <- !is.na(d$age_years) & d$age_years >= 25 & d$age_years <= 64
    age_counts[m] <- sum(d$domain)
    for (v in names(spec$filters)) {
      if (!v %in% names(registry$groups)) stop("Unsupported filter")
      allowed <- vapply(registry$groups[[v]]$categories, function(g) g$id, character(1))
      if (!all(unlist(spec$filters[[v]]) %in% allowed)) stop("Unknown filter category")
      d$domain <- d$domain & as.character(d[[v]]) %in% unlist(spec$filters[[v]])
    }
    # Critical ordering: all adults enter svydesign; only then is the domain subset applied.
    full <- svydesign(ids = ~PPSU, strata = ~PSTRAT, weights = ~WTFA_A, data = d, nest = TRUE)
    full_designs[[m]] <- full
    designs[[m]] <- subset(full, domain)
  }
  rm(datasets)
  count_range <- function(x) list(min = as.integer(min(x)), max = as.integer(max(x)))
  d_n <- vapply(designs, function(d) nrow(d$variables), integer(1))
  if (any(d_n == 0)) return(blocked("empty_domain", "The selected domain has no respondents in at least one imputation."))
  full_df <- degf(full_designs[[1]])
  if (full_df < registry$reliability$minimum_design_df) stop("Insufficient full-sample design degrees of freedom")
  domain_df <- vapply(designs, degf, numeric(1))
  labels <- function(v, id) {
    candidates <- registry$groups[[v]]$categories
    found <- Filter(function(g) identical(g$id, id), candidates)
    if (length(found)) found[[1]]$label else id
  }
  group_variables <- switch(spec$group_by, overall = character(), education_race = c("education", "race"),
                            education = "education", race = "race", sex = "sex", age = "age", income = "income",
                            stop("Unsupported grouping"))
  combinations <- if (!length(group_variables)) list(list()) else {
    levels_list <- lapply(group_variables, function(v) {
      ids <- vapply(registry$groups[[v]]$categories, function(g) g$id, character(1))
      if (v %in% names(spec$filters)) ids <- ids[ids %in% unlist(spec$filters[[v]])]
      ids
    })
    names(levels_list) <- group_variables
    grid <- expand.grid(levels_list, stringsAsFactors = FALSE)
    lapply(seq_len(nrow(grid)), function(i) as.list(grid[i, , drop = FALSE]))
  }
  cell_match <- function(d, combination) {
    yes <- rep(TRUE, nrow(d$variables))
    for (v in names(combination)) yes <- yes & !is.na(d$variables[[v]]) & as.character(d$variables[[v]]) == combination[[v]]
    yes
  }
  # Pool the complete covariance matrices, never average standard errors or p-values.
  pool_scalar <- function(q, u, df_complete) {
    if (length(q) == 1) return(list(estimate = q[1], variance = u[1], df = df_complete,
                                   within_variance = u[1], between_variance = 0, missing_information = 0))
    pooled <- MIcombine(results = lapply(q, function(x) c(estimate = x)),
                        variances = lapply(u, function(x) matrix(x, 1, 1)), df.complete = df_complete)
    list(estimate = unname(coef(pooled)[1]), variance = unname(vcov(pooled)[1, 1]),
         df = unname(pooled$df[1]), within_variance = mean(u), between_variance = var(q),
         missing_information = unname(pooled$missinfo[1]))
  }
  rows <- list()
  internal_cells <- list()
  for (combination in combinations) {
    id <- if (!length(combination)) "overall" else paste(unlist(combination), collapse = "|")
    label <- if (!length(combination)) "Adults aged 25–64" else paste(vapply(names(combination), function(v) labels(v, combination[[v]]), character(1)), collapse = " · ")
    eligible <- n <- events <- numeric()
    estimates <- variances <- dfs <- numeric()
    weight_sums <- numeric()
    cells <- list()
    for (m in seq_along(designs)) {
      d <- designs[[m]]
      yes <- cell_match(d, combination)
      eligible[m] <- sum(yes)
      good <- yes & !is.na(d$variables$outcome_value)
      n[m] <- sum(good)
      events[m] <- sum(d$variables$outcome_value[good])
      weight_sums[m] <- sum(weights(d)[good])
      cell <- d[good, ]
      cells[[m]] <- cell
      dfs[m] <- if (n[m] > 0) degf(cell) else 0
    }
    reasons <- character()
    if (min(n) < spec$min_n) reasons <- c(reasons, paste0("Unweighted denominator below ", spec$min_n))
    if (min(events) < registry$reliability$minimum_events || min(n - events) < registry$reliability$minimum_non_events)
      reasons <- c(reasons, "Fewer than 10 events or 10 non-events in at least one imputation")
    if (min(dfs) < registry$reliability$minimum_design_df) reasons <- c(reasons, "Fewer than 8 design degrees of freedom")
    output <- list(group_id = id, label = label, n = as.integer(min(n)), n_range = count_range(n),
                   eligible_n_range = count_range(eligible), missing_outcome_n_range = count_range(eligible - n),
                   weighted_denominator_range = list(min = min(weight_sums), max = max(weight_sums)),
                   estimate = NULL, se = NULL, ci_low = NULL, ci_high = NULL,
                   status = "suppressed", suppression_reason = NULL, design_df = min(dfs))
    if (!length(reasons)) {
      for (m in seq_along(cells)) {
        mean_fit <- svymean(~outcome_value, cells[[m]], na.rm = TRUE)
        estimates[m] <- as.numeric(coef(mean_fit)[1])
        variances[m] <- as.numeric(vcov(mean_fit)[1, 1])
      }
      pooled <- pool_scalar(estimates, variances, min(dfs))
      p <- pooled$estimate
      se <- sqrt(pooled$variance)
      neff <- if (se > 0) p * (1 - p) / se^2 else 0
      if (!is.finite(neff) || neff < registry$reliability$minimum_effective_n)
        reasons <- c(reasons, "Effective sample size below 30 or invalid variance")
      if (!is.finite(se) || se <= 0 || p <= 0 || p >= 1) reasons <- c(reasons, "Degenerate estimate or standard error")
      if (is.finite(se) && p > 0 && se / p > registry$reliability$maximum_relative_standard_error)
        reasons <- c(reasons, "Relative standard error exceeds 30%")
      interval <- p + c(-1, 1) * qt(.975, df = pooled$df) * se
      if (length(interval) != 2 || any(!is.finite(interval)) || interval[1] < 0 || interval[2] > 1 ||
          interval[1] > p || interval[2] < p) reasons <- c(reasons, "Invalid confidence interval on the probability scale")
      if (all(is.finite(interval)) && diff(interval) > registry$reliability$maximum_absolute_ci_width)
        reasons <- c(reasons, "95% confidence interval width exceeds 30 percentage points")
      if (!length(reasons)) {
        output$estimate <- p; output$se <- se; output$ci_low <- interval[1]; output$ci_high <- interval[2]
        output$status <- "ok"; output$effective_n <- neff; output$rse <- se / p
        output$pooled_df <- pooled$df
        output$imputation <- list(m = length(cells), within_variance = pooled$within_variance,
                                  between_variance = pooled$between_variance, missing_information = pooled$missing_information)
      }
    }
    output$suppression_reason <- if (length(reasons)) paste(unique(reasons), collapse = "; ") else NULL
    rows[[length(rows) + 1L]] <- output
    internal_cells[[id]] <- list(combination = combination, cells = cells, row = output)
  }
  valid_outcome <- vapply(designs, function(d) sum(!is.na(d$variables$outcome_value)), integer(1))
  missingness <- lapply(c("outcome_value", "education", "race", "sex", "age_years"), function(v)
    count_range(vapply(designs, function(d) sum(is.na(d$variables[[v]])), integer(1))))
  names(missingness) <- c("outcome", "education", "race", "sex", "age")
  raw_variables <- unique(c(outcome_variable, "EDUCP_A", "HISPALLP_A", "SEX_A", "AGEP_A"))
  audit_missing_codes <- function(frames) {
    audit <- lapply(raw_variables, function(v) {
      special <- if (v %in% c(outcome_variable, "SEX_A")) c(7, 8, 9) else c(97, 98, 99)
      valid <- if (v == outcome_variable) c(one_codes, zero_codes) else
        switch(v, EDUCP_A = 0:10, HISPALLP_A = 1:7, SEX_A = 1:2, AGEP_A = 18:85)
      registered_missing <- if (v == outcome_variable) missing_codes else special
      count_code <- function(code) count_range(vapply(frames, function(f) sum(f[[v]] %in% code), integer(1)))
      list(refused = list(code = special[1], n_range = count_code(special[1])),
           not_ascertained = list(code = special[2], n_range = count_code(special[2])),
           dont_know = list(code = special[3], n_range = count_code(special[3])),
           registered_missing_codes = as.list(registered_missing),
           other_registered_missing_n_range = count_code(setdiff(registered_missing, special)),
           blank_n_range = count_range(vapply(frames, function(f) sum(is.na(f[[v]])), integer(1))),
           other_invalid_n_range = count_range(vapply(frames, function(f)
             sum(!is.na(f[[v]]) & !f[[v]] %in% c(valid, registered_missing)), integer(1))))
    })
    names(audit) <- raw_variables
    audit
  }
  age_eligible <- !is.na(base$age_years) & base$age_years >= 25 & base$age_years <= 64
  missingness_by_code <- list(full_sample = audit_missing_codes(list(base)),
    age_domain = audit_missing_codes(list(base[age_eligible, , drop = FALSE])),
    filtered_domain = audit_missing_codes(lapply(designs, function(d) d$variables)),
    note = "Age-unknown records cannot enter the age domain; full-sample age missingness is therefore retained separately. Ranges span income imputations when relevant.")
  answer <- list(status = "ok", rows = rows, outcome = c(list(id = spec$outcome), outcome),
                 denominators = list(full_sample_n = nrow(base), age_domain_n = as.integer(age_counts[1]),
                                     filtered_domain_n = as.integer(min(d_n)), filtered_domain_n_range = count_range(d_n),
                                     valid_outcome_n = as.integer(min(valid_outcome)), valid_outcome_n_range = count_range(valid_outcome),
                                     explanation = "n is unweighted; income analyses report the minimum across imputations and n_range. Respondents are not counted ten times."),
                 missingness = missingness,
                 missingness_by_code = missingness_by_code,
                 design = list(weight = "WTFA_A", strata = "PSTRAT", psu = "PPSU", nest = TRUE,
                               full_sample_n = nrow(base), full_design_df = full_df,
                               domain_design_df_range = list(min = min(domain_df), max = max(domain_df)),
                               variance_method = "Taylor linearization; with-replacement first-stage variance", finite_population_correction = FALSE,
                               domain = "25 <= AGEP_A <= 64, followed by categorical filters", constructed_before_domain_subset = TRUE,
                               lonely_psu_policy = "fail for single-PSU full-sample strata"),
                 imputation = list(required = requires_mi, m = length(designs),
                                   method = if (requires_mi) "Rubin pooling via mitools::MIcombine; finite complete-data df, Barnard-Rubin adjustment" else "Not applicable",
                                   source = if (requires_mi) "Official adultinc24.csv; all 10 imputations merged one-to-one by HHX within imputation" else NULL),
                 methods = list(estimator = "Survey-weighted ratio mean of binary outcome", confidence_level = .95,
                                proportion_ci = if (requires_mi) "Rubin pooled t-Wald interval on the probability scale; invalid intervals suppressed, not clipped" else "Design-based t-Wald interval using cell-domain degrees of freedom; invalid intervals suppressed, not clipped",
                                units = "Proportions on 0–1 scale; risk differences are also on the 0–1 scale",
                                reliability_policy = registry$reliability,
                                interpretation = "Cross-sectional descriptive associations; not causal effects; no multiplicity adjustment"),
                 checks = list(list(name = "full_design_before_domain_subset", status = "passed"),
                               list(name = "all_ten_income_imputations_and_unique_household_merges", status = if (requires_mi) "passed" else "not_applicable"),
                               list(name = "cell_reliability_gates", status = "passed", suppressed_cells = sum(vapply(rows, function(r) r$status == "suppressed", logical(1))))))
  # Contrasts use a joint covariance matrix, retaining covariance between groups.
  if (spec$analysis == "gradient") {
    contrasts <- list()
    gradient_variable <- if (spec$group_by == "income") "income" else "education"
    reference <- if (gradient_variable == "income") "400_plus" else "bachelors_plus"
    reference_label <- labels(gradient_variable, reference)
    race_ids <- if (spec$group_by == "education_race") vapply(registry$groups$race$categories, function(g) g$id, character(1)) else NA_character_
    for (race_id in race_ids) {
      target_ids <- if (gradient_variable == "income") c("below_100", "100_199", "200_399") else
        c("less_than_high_school", "high_school", "some_college")
      for (target in target_ids) {
        reference_id <- if (is.na(race_id)) reference else paste(reference, race_id, sep = "|")
        target_id <- if (is.na(race_id)) target else paste(target, race_id, sep = "|")
        if (!all(c(reference_id, target_id) %in% names(internal_cells))) next
        available <- internal_cells[[reference_id]]$row$status == "ok" && internal_cells[[target_id]]$row$status == "ok"
        item <- list(group_id = target_id, reference_id = reference_id,
                     label = paste(labels(gradient_variable, target), "minus", reference_label, if (!is.na(race_id)) paste("·", labels("race", race_id)) else ""),
                     estimate = NULL, se = NULL, ci_low = NULL, ci_high = NULL,
                     status = "suppressed", suppression_reason = "A component estimate was suppressed")
        if (available) {
          q <- u <- dfc <- numeric()
          for (m in seq_along(designs)) {
            d <- designs[[m]]
            keep <- !is.na(d$variables[[gradient_variable]]) & !is.na(d$variables$outcome_value)
            if (!is.na(race_id)) keep <- keep & !is.na(d$variables$race) & as.character(d$variables$race) == race_id
            d <- d[keep, ]
            fit <- svyby(~outcome_value, as.formula(paste("~", gradient_variable)), d, FUN = svymean, na.rm = TRUE,
                         covmat = TRUE, keep.var = TRUE, drop.empty.groups = TRUE)
            ids <- as.character(fit[[gradient_variable]])
            a <- match(target, ids); b <- match(reference, ids)
            covariance <- vcov(fit)
            q[m] <- fit$outcome_value[a] - fit$outcome_value[b]
            u[m] <- covariance[a, a] + covariance[b, b] - 2 * covariance[a, b]
            dfc[m] <- degf(d)
          }
          pooled <- pool_scalar(q, u, min(dfc))
          interval <- pooled$estimate + c(-1, 1) * qt(.975, pooled$df) * sqrt(pooled$variance)
          if (all(is.finite(c(interval, pooled$variance))) && pooled$variance > 0) {
            item$estimate <- pooled$estimate; item$se <- sqrt(pooled$variance)
            item$ci_low <- interval[1]; item$ci_high <- interval[2]
            item$status <- "ok"; item$suppression_reason <- NULL; item$df <- pooled$df
          } else item$suppression_reason <- "Invalid contrast variance or confidence interval"
        }
        contrasts[[length(contrasts) + 1L]] <- item
      }
    }
    answer$gradient <- list(measure = "risk_difference", reference = reference, contrasts = contrasts,
                            interpretation = paste("Unadjusted difference from", reference_label, "; confidence intervals account for joint survey covariance and income imputations when required"))
    if (!length(contrasts) || !any(vapply(contrasts, function(x) x$status == "ok", logical(1)))) {
      answer$status <- "blocked"; answer$code <- "no_releasable_gradient"
      answer$reason <- "No requested gradient contrast could be released; its reference or comparison cells are unavailable or suppressed."
      answer$rows <- list()
      return(answer)
    }
  }
  if (spec$analysis == "regression") {
    formula <- fixed_model_formula(adjust_income)
    formula_text <- paste(spec$outcome, "~", paste(attr(terms(formula), "term.labels"), collapse = " + "))
    block_model <- function(reason) {
      answer$status <- "blocked"; answer$code <- "regression_gate_failed"; answer$reason <- reason
      answer$rows <- list()
      answer$regression <- list(status = "blocked", reason = reason, formula = formula_text)
      answer
    }
    fits <- unadjusted_fits <- list(); model_n <- model_events <- numeric()
    fit_valid <- function(fit, n, events) {
      pcount <- length(coef(fit))
      isTRUE(fit$converged) && fit$rank == pcount && all(is.finite(coef(fit))) &&
        all(is.finite(vcov(fit))) && all(abs(coef(fit)) <= 20) &&
        min(events, n - events) >= 10 * pcount && fit$df.residual >= 8
    }
    for (m in seq_along(designs)) {
      d <- designs[[m]]
      use <- complete.cases(d$variables[all.vars(formula)])
      d <- d[use, ]
      model_n[m] <- nrow(d$variables); model_events[m] <- sum(d$variables$outcome_value)
      if (model_n[m] < spec$min_n) return(block_model("Insufficient complete-case model denominator"))
      factors <- c("education", "age", "sex", "race", if (adjust_income) "income")
      for (v in factors) {
        d$variables[[v]] <- droplevels(d$variables[[v]])
        ref <- registry$regression$references[[v]]
        if (nlevels(d$variables[[v]]) < 2 || !ref %in% levels(d$variables[[v]]))
          return(block_model(paste("Model factor", v, "lacks variation or its prespecified reference category in the filtered domain")))
        counts <- table(d$variables[[v]], d$variables$outcome_value)
        if (ncol(counts) != 2 || any(counts < 10))
          return(block_model(paste("Sparse model cells in", v, "(<10 events/non-events); separation risk")))
        d$variables[[v]] <- relevel(d$variables[[v]], ref = ref)
      }
      # Both models use this identical complete-case survey domain, within every imputation.
      fit <- svyglm(formula, design = d, family = quasibinomial(link = "logit"))
      unadjusted <- svyglm(outcome_value ~ education, design = d, family = quasibinomial(link = "logit"))
      if (!fit_valid(fit, model_n[m], model_events[m]) || !fit_valid(unadjusted, model_n[m], model_events[m]))
        return(block_model("Adjusted or unadjusted model failed convergence, rank, coefficient stability, events-per-parameter, or residual-design-df gate"))
      fits[[m]] <- fit; unadjusted_fits[[m]] <- unadjusted
    }
    summarise_fits <- function(collection) {
      if (!all(vapply(collection, function(f) identical(names(coef(f)), names(coef(collection[[1]]))), logical(1))))
        stop("Regression coefficients do not align across imputations")
      complete_df <- min(vapply(collection, function(f) f$df.residual, numeric(1)))
      if (length(collection) > 1) {
        pooled <- MIcombine(collection, df.complete = complete_df)
        beta <- coef(pooled); variance <- vcov(pooled); dfs <- pooled$df; fraction <- pooled$missinfo
      } else {
        beta <- coef(collection[[1]]); variance <- vcov(collection[[1]])
        dfs <- rep(complete_df, length(beta)); fraction <- rep(0, length(beta))
      }
      ses <- sqrt(diag(variance)); low <- beta - qt(.975, dfs) * ses; high <- beta + qt(.975, dfs) * ses
      if (any(!is.finite(c(beta, ses, exp(low), exp(high)))) || any(ses <= 0))
        stop("Nonfinite or degenerate pooled regression estimate or interval")
      lapply(seq_along(beta), function(j) list(term = names(beta)[j], log_odds = unname(beta[j]),
        se = unname(ses[j]), odds_ratio = unname(exp(beta[j])), ci_low = unname(exp(low[j])),
        ci_high = unname(exp(high[j])), df = unname(dfs[j]), missing_information = unname(fraction[j])))
    }
    adjusted_coefficients <- summarise_fits(fits)
    unadjusted_coefficients <- summarise_fits(unadjusted_fits)
    answer$regression <- list(status = "ok", formula = formula_text, coefficients = adjusted_coefficients,
      n = as.integer(min(model_n)), n_range = count_range(model_n),
      complete_case_exclusions_range = count_range(d_n - model_n),
      references = registry$regression$references,
      adjustment = if (adjust_income) "Education, age group, sex, race/ethnicity, and income-to-poverty category; all 10 income imputations pooled" else "Explicit reduced model: education, age group, sex, and race/ethnicity; income excluded (income domains still require all 10 imputations)",
      income_adjusted = adjust_income, imputation_count = length(fits),
      unadjusted = list(formula = paste(spec$outcome, "~ education"), coefficients = unadjusted_coefficients,
        n = as.integer(min(model_n)), n_range = count_range(model_n),
        sample = "Identical complete-case domain as the adjusted model within each imputation"),
      variance_method = "Design-based sandwich covariance; pooled log-odds and complete covariance matrices when multiply imputed",
      interpretation = "Adjusted odds ratios, not risk ratios or causal effects. Complete cases for non-income covariates; residual confounding and model specification remain limitations.")
    answer$checks <- c(answer$checks, list(list(name = "unadjusted_adjusted_identical_complete_case_domain", status = "passed"),
      list(name = "all_imputation_regression_convergence_rank_sparse_cell_and_df_gates", status = "passed")))
    # Regression output is carried only in its typed object, not in unadjusted prevalence rows.
    answer$rows <- list()
    return(answer)
  }
  if (!any(vapply(rows, function(r) r$status == "ok", logical(1)))) {
    answer$status <- "blocked"; answer$code <- "all_cells_suppressed"
    answer$reason <- "Every requested estimate failed a reliability gate; no estimates are released."
  }
  answer
}
result <- tryCatch(withCallingHandlers(main(), warning = function(w) {
  warning_log <<- unique(c(warning_log, conditionMessage(w)))
  invokeRestart("muffleWarning")
}), error = function(e) blocked("survey_engine_error", conditionMessage(e)))
result$warnings <- as.list(warning_log)
result$engine <- list(name = "R survey", R = as.character(getRversion()),
                      survey = if (requireNamespace("survey", quietly = TRUE)) as.character(packageVersion("survey")) else NULL,
                      mitools = if (requireNamespace("mitools", quietly = TRUE)) as.character(packageVersion("mitools")) else NULL)
write_result(result)
