#!/usr/bin/env Rscript
# Independent test-only reference: ratios of multivariate survey totals and
# analytic delta gradients, rather than the production svymean/svyby path.
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 4L) stop("Usage: reference_survey.R synthetic-adult.csv synthetic-income.csv cases.json reference.json")
suppressPackageStartupMessages(library(survey))
suppressPackageStartupMessages(library(jsonlite))
options(survey.lonely.psu = "fail", survey.adjust.domain.lonely = FALSE)
adult <- read.csv(args[1], stringsAsFactors = FALSE, na.strings = c("", "NA"))
income <- read.csv(args[2], stringsAsFactors = FALSE)
cases <- read_json(args[3], simplifyVector = FALSE)
levels_for <- list(education = c("less_than_high_school", "high_school", "some_college", "bachelors_plus"),
                   race = c("hispanic", "nh_white", "nh_black", "nh_asian", "nh_other"),
                   sex = c("male", "female"), age = c("25_34", "35_44", "45_54", "55_64"),
                   income = c("below_100", "100_199", "200_399", "400_plus"))
factor_codes <- function(raw, codes, levels) {
  value <- rep(NA_character_, length(raw))
  for (i in seq_along(codes)) value[raw %in% codes[[i]]] <- levels[i]
  factor(value, levels = levels)
}
base_frame <- function(spec, m = NULL) {
  d <- adult
  mapping <- switch(spec$outcome,
    fair_poor = list(variable = "PHSTAT_A", one = c(4, 5), zero = 1:3),
    insured = list(variable = "NOTCOV_A", one = 2, zero = 1),
    uninsured = list(variable = "NOTCOV_A", one = 1, zero = 2),
    delayed_care_cost = list(variable = "MEDDL12M_A", one = 1, zero = 2),
    unmet_care_cost = list(variable = "MEDNG12M_A", one = 1, zero = 2), stop("Unsupported fixture outcome"))
  raw <- d[[mapping$variable]]
  d$y <- ifelse(raw %in% mapping$one, 1, ifelse(raw %in% mapping$zero, 0, NA_real_))
  d$education <- factor_codes(d$EDUCP_A, list(0:2, 3:4, 5:7, 8:10), levels_for$education)
  d$race <- factor_codes(d$HISPALLP_A, list(1, 2, 3, 4, 5:7), levels_for$race)
  d$sex <- factor_codes(d$SEX_A, list(1, 2), levels_for$sex)
  d$age <- factor_codes(d$AGEP_A, list(25:34, 35:44, 45:54, 55:64), levels_for$age)
  if (!is.null(m)) {
    imputation <- income[income$IMPNUM_A == m, ]
    d$income <- factor_codes(imputation$RATCAT_A[match(d$HHX, imputation$HHX)], list(1:3, 4:7, 8:11, 12:14), levels_for$income)
  }
  d$domain <- !is.na(d$AGEP_A) & d$AGEP_A >= 25 & d$AGEP_A <= 64
  for (name in names(spec$filters)) d$domain <- d$domain & as.character(d[[name]]) %in% unlist(spec$filters[[name]])
  d
}
manual_pool <- function(q, u, df) {
  m <- length(q); W <- mean(u); B <- if (m > 1L) var(q) else 0
  total <- W + (1 + 1/m) * B
  if (m > 1L) {
    # Registered mitools finite-complete-data implementation, independently
    # expressed here (including its W/(W+B) observed-df adjustment).
    r <- (1 + 1/m) * B/W
    old <- if (r == 0) Inf else (m - 1) * (1 + 1/r)^2
    observed <- ((df + 1)/(df + 3)) * df * W/(W + B)
    df <- 1/(1/old + 1/observed)
  }
  estimate <- mean(q); se <- sqrt(total)
  interval <- estimate + c(-1, 1) * qt(.975, df) * se
  list(estimate = estimate, variance = total, se = se, ci_low = interval[1], ci_high = interval[2],
       df = df, W = W, B = B, mean_se = mean(sqrt(u)), estimates = q, variances = u)
}
joint_ratios <- function(full, combinations) {
  d <- full$variables; k <- length(combinations); formula_names <- character(); counts <- dfs <- numeric()
  for (j in seq_len(k)) {
    keep <- d$domain & !is.na(d$y)
    for (name in names(combinations[[j]])) keep <- keep & !is.na(d[[name]]) & as.character(d[[name]]) == combinations[[j]][[name]]
    numerator <- paste0("N", j); denominator <- paste0("D", j)
    full$variables[[numerator]] <- ifelse(keep, ifelse(is.na(d$y), 0, d$y), 0)
    full$variables[[denominator]] <- as.numeric(keep)
    formula_names <- c(formula_names, numerator, denominator)
    counts[j] <- sum(keep); dfs[j] <- if (counts[j]) degf(full[keep, ]) else 0
  }
  totals <- svytotal(reformulate(formula_names), full)
  values <- coef(totals); covariance <- vcov(totals)
  jacobian <- matrix(0, k, length(values)); ratios <- numeric()
  for (j in seq_len(k)) {
    a <- match(paste0("N", j), names(values)); b <- match(paste0("D", j), names(values))
    ratios[j] <- values[a]/values[b]
    jacobian[j, a] <- 1/values[b]; jacobian[j, b] <- -values[a]/values[b]^2
  }
  list(q = ratios, covariance = jacobian %*% covariance %*% t(jacobian), n = counts, df = dfs)
}
references <- list()
for (entry in cases) {
  spec <- entry$spec
  mi <- spec$group_by == "income" || "income" %in% names(spec$filters) || (spec$analysis == "regression" && spec$adjustment == "full")
  frames <- lapply(if (mi) 1:10 else list(NULL), function(m) base_frame(spec, m))
  designs <- lapply(frames, function(d) svydesign(ids = ~PPSU, strata = ~PSTRAT, weights = ~WTFA_A, data = d, nest = TRUE))
  variables <- switch(spec$group_by, overall = character(), education_race = c("education", "race"), spec$group_by)
  combinations <- if (!length(variables)) list(list()) else {
    choices <- lapply(variables, function(name) if (name %in% names(spec$filters)) unlist(spec$filters[[name]]) else levels_for[[name]])
    names(choices) <- variables
    grid <- expand.grid(choices, stringsAsFactors = FALSE)
    lapply(seq_len(nrow(grid)), function(i) as.list(grid[i, , drop = FALSE]))
  }
  ids <- vapply(combinations, function(x) if (length(x)) paste(unlist(x), collapse = "|") else "overall", character(1))
  moments <- lapply(designs, joint_ratios, combinations = combinations)
  cells <- lapply(seq_along(ids), function(j) {
    value <- manual_pool(vapply(moments, function(x) x$q[j], numeric(1)), vapply(moments, function(x) x$covariance[j, j], numeric(1)), min(vapply(moments, function(x) x$df[j], numeric(1))))
    c(list(group_id = ids[j], n = min(vapply(moments, function(x) x$n[j], numeric(1)))), value)
  })
  output <- list(rows = cells, full_design_df = degf(designs[[1]]),
    filtered_domain_n = min(vapply(frames, function(d) sum(d$domain), numeric(1))),
    valid_outcome_n = min(vapply(frames, function(d) sum(d$domain & !is.na(d$y)), numeric(1))),
    imputation_count = length(frames),
    wrong_filtered_design_fails = inherits(try({
      wrong <- svydesign(ids = ~PPSU, strata = ~PSTRAT, weights = ~WTFA_A, data = frames[[1]][frames[[1]]$domain, ], nest = TRUE)
      svymean(~y, wrong, na.rm = TRUE)
    }, silent = TRUE), "try-error"))
  if (spec$analysis == "gradient") {
    variable <- if (spec$group_by == "income") "income" else "education"
    reference <- if (variable == "income") "400_plus" else "bachelors_plus"
    b <- match(reference, ids)
    output$contrasts <- lapply(setdiff(seq_along(ids), b), function(a) {
      q <- vapply(moments, function(x) x$q[a] - x$q[b], numeric(1))
      variance <- vapply(moments, function(x) x$covariance[a, a] + x$covariance[b, b] - 2 * x$covariance[a, b], numeric(1))
      covariance <- vapply(moments, function(x) x$covariance[a, b], numeric(1))
      df <- min(vapply(designs, function(full) {
        d <- full$variables; degf(full[d$domain & !is.na(d$y) & !is.na(d[[variable]]), ])
      }, numeric(1)))
      c(list(group_id = ids[a], reference_id = ids[b], cross_covariances = covariance,
             independent_group_variance = mean(vapply(moments, function(x) x$covariance[a, a] + x$covariance[b, b], numeric(1)))), manual_pool(q, variance, df))
    })
  }
  if (spec$analysis == "regression") {
    adjusted <- unadjusted <- list(); sample_counts <- naive_counts <- numeric()
    covariates <- c("education", "age", "sex", "race", if (spec$adjustment == "full") "income")
    reference_levels <- c(education = "bachelors_plus", age = "25_34", sex = "female", race = "nh_white", income = "400_plus")
    for (i in seq_along(designs)) {
      full <- designs[[i]]
      for (v in covariates) full$variables[[v]] <- relevel(full$variables[[v]], reference_levels[v])
      d <- full$variables; complete <- d$domain & complete.cases(d[c("y", covariates)])
      same <- full[complete, ]; sample_counts[i] <- sum(complete)
      naive_counts[i] <- sum(d$domain & !is.na(d$y) & !is.na(d$education))
      adjusted[[i]] <- svyglm(reformulate(covariates, response = "y"), design = same, family = quasibinomial())
      unadjusted[[i]] <- svyglm(y ~ education, design = same, family = quasibinomial())
    }
    pool_models <- function(fits) {
      beta <- do.call(rbind, lapply(fits, coef)); covariance <- lapply(fits, vcov)
      W <- Reduce("+", covariance)/length(fits); B <- if (length(fits) > 1) cov(beta) else W * 0
      total <- W + (1 + 1/length(fits)) * B
      coefficients <- lapply(seq_len(ncol(beta)), function(j) {
        pooled <- manual_pool(beta[, j], vapply(covariance, function(x) x[j, j], numeric(1)), min(vapply(fits, function(x) x$df.residual, numeric(1))))
        list(term = colnames(beta)[j], log_odds = pooled$estimate, se = pooled$se, df = pooled$df,
             odds_ratio = exp(pooled$estimate), ci_low = exp(pooled$ci_low), ci_high = exp(pooled$ci_high))
      })
      list(coefficients = coefficients, total_covariance = unname(total), all_converged = all(vapply(fits, function(fit) isTRUE(fit$converged), logical(1))))
    }
    output$regression <- list(n = min(sample_counts), per_imputation_n = sample_counts, naive_unadjusted_n = naive_counts,
                             adjusted = pool_models(adjusted), unadjusted = pool_models(unadjusted))
  }
  references[[entry$name]] <- output
}
write_json(list(fixture_only = TRUE, reference_method = "Multivariate survey totals with analytic ratio/contrast delta gradients; manually expressed finite-df Rubin pooling",
                runtime = list(R = R.version.string, survey = as.character(packageVersion("survey")), jsonlite = as.character(packageVersion("jsonlite")), mitools = as.character(packageVersion("mitools"))),
                cases = references), args[4], pretty = TRUE, auto_unbox = TRUE, digits = 15, null = "null", na = "null")
