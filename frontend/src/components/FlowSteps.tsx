import { Link } from "react-router-dom";

export interface FlowStep {
  title: string;
  description: string;
  to: string;
  linkLabel: string;
}

interface FlowStepsProps {
  steps: FlowStep[];
}

/**
 * Ürünün temel akışını numaralı adımlar olarak gösterir (ör. genel bakış
 * sayfası). Her adım kendi hedefine giden bir bağlantı taşır; sıra numarası
 * dekoratiftir (`aria-hidden`) ve başlığın erişilebilir adına karışmaz.
 */
export function FlowSteps({ steps }: FlowStepsProps) {
  return (
    <ol className="flow-steps">
      {steps.map((step, index) => (
        <li className="flow-step" key={step.title}>
          <span className="flow-step__badge" aria-hidden="true">
            {index + 1}
          </span>
          <div className="flow-step__body">
            <h3 className="flow-step__title">{step.title}</h3>
            <p className="flow-step__description">{step.description}</p>
            <Link to={step.to} className="flow-step__link">
              {step.linkLabel}
            </Link>
          </div>
        </li>
      ))}
    </ol>
  );
}
