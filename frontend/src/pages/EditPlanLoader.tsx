// create react FC component called EditPlanLoader
import React, { FC, Fragment, useEffect } from "react";
import { useParams } from "react-router-dom";
import { useQuery } from "react-query";
import { Plan } from "../api/api";
import { PlanDetailType } from "../types/plan-type";
import LoadingSpinner from "../components/LoadingSpinner";
import { usePlanState } from "../context/PlanContext";
import { useNavigate } from "react-router-dom";
import { Button } from "antd";
import EditPlan from "./EditPlan";

type PlanDetailParams = {
  planId: string;
};

interface EditPlanLoaderProps {
  type: "backtest" | "version" | "custom";
}

const EditPlanLoader = ({ type }: EditPlanLoaderProps) => {
  const navigate = useNavigate();
  const { planId } = useParams<PlanDetailParams>();
  const [versionIndex, setVersionIndex] = React.useState<number>();
  const { replacementPlanVersion } = usePlanState();

  const {
    data: plan,
    isLoading,
    isError,
  } = useQuery<PlanDetailType>(
    ["plan_detail", planId],
    () =>
      Plan.getPlan(planId).then((res) => {
        return res;
      }),

    {
      onSuccess: (data) => {},
    }
  );

  useEffect(() => {
    if (plan !== undefined) {
      if (type === "backtest") {
        setVersionIndex(
          plan.versions.findIndex(
            (v) => v.version_id === replacementPlanVersion?.version_id
          )
        );
      } else {
        setVersionIndex(plan.versions.findIndex((x) => x.status === "active"));
      }
    }
  }, [plan]);

  return (
    <Fragment>
      {isLoading && <LoadingSpinner />}
      {isError && (
        <div className="flex flex-col items-center justify-center h-full">
          <h2 className="mb-5">Could Not Load Plan</h2>
          <Button type="primary" onClick={() => navigate(-1)}>
            Go Back
          </Button>
        </div>
      )}
      {plan !== undefined && versionIndex !== undefined && (
        <EditPlan type={type} plan={plan} versionIndex={versionIndex} />
      )}
    </Fragment>
  );
};
export default EditPlanLoader;
